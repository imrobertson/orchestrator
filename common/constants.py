"""
Shared constants for the cluster orchestrator scripts.

ContainerRole's values are the literal container names used for `docker run
--name` / `docker ps` / `docker logs` / `docker exec` across
dgx-orchestrator.py, cache_cluster_assets.py, and benchmark.py. They MUST
match cluster_config.yaml's `container_names` block exactly - the assertion
below runs at import time so the two can never silently diverge.
"""

from __future__ import annotations

from enum import StrEnum

from common.config import load_cluster_config


class ContainerRole(StrEnum):
    STANDALONE = "vllm-standalone"
    HEAD = "vllm-head"
    WORKER = "vllm-worker"


def _assert_matches_cluster_config() -> None:
    """
    Verify ContainerRole's values agree with cluster_config.yaml's
    container_names block. Raises AssertionError naming the mismatch if the
    two have drifted apart.
    """
    expected = {
        "standalone": ContainerRole.STANDALONE.value,
        "head": ContainerRole.HEAD.value,
        "worker": ContainerRole.WORKER.value,
    }
    actual = load_cluster_config().container_names
    assert actual == expected, (
        f"ContainerRole values {expected} do not match cluster_config.yaml's "
        f"container_names block {actual}. Update common/constants.py or "
        f"cluster_config.yaml so they agree."
    )


_assert_matches_cluster_config()
