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
from pydantic import BaseModel, Field, ValidationError

# --- Path resolution (mirrors the BASE_DIR pattern used by the existing
# scripts, e.g. dgx-orchestrator.py: Path(os.getenv("BASE_DIR", <repo root>)).
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

    # Marks a host as carrying something long-lived that must not be
    # disturbed by routine work -- a resident agent, a shared endpoint the
    # team depends on. Deploy and teardown both refuse to touch a reserved
    # host unless explicitly forced.
    #
    # This is deliberately a property of the NODE, not of a deployment:
    # unlike `role`, which describes a host's part in one particular
    # topology and should eventually move to the deployment record, being
    # reserved is a statement about what the machine is FOR. It stays
    # meaningful under an N-node generalization where any node can be head
    # or worker.
    #
    # Defaults false, so an existing cluster_config.yaml that omits it
    # behaves exactly as before.
    reserved: bool = False


class NetworkConfig(BaseModel):
    topology: str
    interface: str
    nccl_ib_hca: str


class TuningConfig(BaseModel):
    """
    Deploy-time tuning knobs previously hardcoded as literals inside
    dgx-orchestrator.py's _execute_deployment_impl(). All fields have
    defaults matching those old hardcoded values, so a cluster_config.yaml
    that omits the `tuning:` section entirely (e.g. an older file, or a
    test fixture) keeps working exactly as before with no change required.
    """
    shm_size_1node: str = "16gb"
    shm_size_2node: str = "64gb"
    gpu_clock_lock: str = "300,1800"
    deploy_wait_timeout_sec: int = 900
    deploy_poll_interval_sec: int = 15
    jit_cache_maxsize_bytes: int = 10_737_418_240  # 10GB

    # Opt-in debug mode for chasing silent worker deaths (e.g. an unlogged
    # RayWorkerProc crash from a bad CUDA kernel launch): forces synchronous
    # kernel launches so a fault raises at its actual call site instead of
    # surfacing later as an opaque "died unexpectedly". Costs real decode
    # throughput -- leave false for normal serving.
    debug_launch_blocking: bool = False

    # Age-based retention for the per-deploy Ray/crash log dirs persisted
    # under ~/.cache/ray-logs/<deploy_run_id>/<host> (see
    # dgx-orchestrator.py's _jit_cache_mounts_and_env). Consumed by
    # prune_cluster_ray_logs(); unlike JIT cache eviction this is not tied
    # to a free-space floor since these logs are tiny by comparison.
    crash_log_retention_days: int = 7


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

    # Which host an unqualified 1-node operation should land on.
    #
    # Previously every "default host" resolved to the first entry under
    # `hosts:` (dgx-orchestrator.py's PRIMARY_HOST). That single variable
    # was doing two unrelated jobs: naming the structural head node for
    # multi-node deploys and the telemetry-authoritative serving host, AND
    # serving as the fallback target for anything that didn't name a host.
    # Those pull in opposite directions the moment one node holds something
    # long-lived -- the node you most want to be authoritative is then also
    # the node a bare `deploy` would flatten.
    #
    # Setting this splits the two. It affects 1-node defaults only; a
    # 2-node deploy still puts the head on the first listed host, so the
    # head stays consistent across topologies.
    #
    # Omit it (None) and everything falls back to the first active host,
    # i.e. the pre-existing behaviour.
    default_deploy_target: Optional[str] = None

    # See TuningConfig above. default_factory (not a bare instance) so each
    # ClusterConfig that omits `tuning:` gets its own TuningConfig() rather
    # than sharing one mutable default across every load.
    tuning: TuningConfig = Field(default_factory=TuningConfig)


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
        cfg = ClusterConfig(**data)
    except ValidationError as exc:
        raise ValueError(
            f"Cluster config file {config_path} failed validation: {exc}"
        ) from exc

    # Cross-field checks live here rather than in a pydantic validator so
    # this module stays agnostic about pydantic v1 vs v2 validator APIs,
    # and so the error text keeps the existing "name the file and the
    # specific problem" convention.
    target = cfg.default_deploy_target
    if target is not None:
        active = {name for name, host in cfg.hosts.items() if host.active}
        if target not in cfg.hosts:
            raise ValueError(
                f"Cluster config file {config_path}: default_deploy_target "
                f"{target!r} is not a known host. Known hosts: "
                f"{sorted(cfg.hosts)}."
            )
        if target not in active:
            raise ValueError(
                f"Cluster config file {config_path}: default_deploy_target "
                f"{target!r} names a host with active: false. Unqualified "
                f"deploys would target a host that is not in service."
            )
        if cfg.hosts[target].reserved:
            # Self-defeating rather than merely odd: every unqualified
            # deploy would land on the one host that then refuses it,
            # so nothing would work without --force.
            raise ValueError(
                f"Cluster config file {config_path}: default_deploy_target "
                f"{target!r} is also marked reserved: true. A reserved host "
                f"cannot be the default target for unqualified deploys."
            )

    return cfg


def active_hosts(path: Optional[Path] = None) -> dict[str, HostConfig]:
    """Return only hosts where active is true."""
    cfg = load_cluster_config(path)
    return {name: host for name, host in cfg.hosts.items() if host.active}


def default_deploy_target(path: Optional[Path] = None) -> str:
    """
    The host an unqualified 1-node operation should target.

    Falls back to the first active host -- the pre-existing PRIMARY_HOST
    behaviour -- when cluster_config.yaml does not set
    default_deploy_target. Validation of the configured value happens in
    load_cluster_config(), so by the time it is returned here it is known
    to name an active, non-reserved host.
    """
    cfg = load_cluster_config(path)
    if cfg.default_deploy_target is not None:
        return cfg.default_deploy_target
    return next(iter(active_hosts(path)), "")


def reserved_hosts(path: Optional[Path] = None) -> list[str]:
    """Names of active hosts marked reserved, in cluster_config.yaml order."""
    return [name for name, host in active_hosts(path).items() if host.reserved]


def legacy_hosts_dict(path: Optional[Path] = None) -> dict[str, dict]:
    """
    Backwards-compatibility shim matching the shape of the hardcoded HOSTS
    dict in dgx-orchestrator.py:

        {"spark-4": {"ip": ..., "alias": ..., "role": ...}, ...}

    Built from active hosts only, with `ip` sourced from `management_ip`.

    `reserved` is carried through so callers holding only this dict (rather
    than the typed config) can still see it -- note that this shim builds a
    fixed set of keys, so anything not listed here is invisible downstream
    no matter what cluster_config.yaml says.
    """
    return {
        name: {
            "ip": host.management_ip,
            "alias": host.alias,
            "role": host.role,
            "reserved": host.reserved,
        }
        for name, host in active_hosts(path).items()
    }
