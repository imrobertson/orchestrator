"""Typed deployment-stage outcomes shared by orchestration surfaces.

The deploy path historically returned ad-hoc dictionaries and relied on each
caller to remember which return codes, booleans, and nested result shapes meant
failure. This module makes that contract explicit without coupling it to SSH,
Docker, FastAPI, or the current two-node topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Mapping, Optional


class DeploymentStage(StrEnum):
    VALIDATION = "validation"
    OPERATION_LOCK = "operation_lock"
    PRE_DEPLOY_TEARDOWN = "pre_deploy_teardown"
    HOST_PREPARATION = "host_preparation"
    MOD_RESOLUTION = "mod_resolution"
    CONTAINER_LAUNCH = "container_launch"
    RAY_CLUSTER_READY = "ray_cluster_ready"
    ENGINE_LAUNCH = "engine_launch"
    CONTAINER_SURVIVAL = "container_survival"
    STATE_RECORDING = "state_recording"
    READINESS = "readiness"
    BENCHMARK = "benchmark"


StageStatus = Literal["success", "error", "skipped"]


@dataclass(frozen=True)
class DeploymentStageResult:
    """The complete outcome of one required or optional deployment stage."""

    stage: DeploymentStage
    status: StageStatus
    message: str
    host: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status in ("success", "skipped")

    @classmethod
    def success(
        cls,
        stage: DeploymentStage,
        message: str,
        *,
        host: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> "DeploymentStageResult":
        return cls(stage, "success", message, host, details or {})

    @classmethod
    def failure(
        cls,
        stage: DeploymentStage,
        message: str,
        *,
        host: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> "DeploymentStageResult":
        return cls(stage, "error", message, host, details or {})

    @classmethod
    def skipped(
        cls,
        stage: DeploymentStage,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> "DeploymentStageResult":
        return cls(stage, "skipped", message, None, details or {})

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stage": self.stage.value,
            "status": self.status,
            "message": self.message,
        }
        if self.host is not None:
            result["host"] = self.host
        if self.details:
            result["details"] = dict(self.details)
        return result


class DeploymentProgress:
    """Collect stage results and build a self-consistent public response."""

    def __init__(self) -> None:
        self._stages: list[DeploymentStageResult] = []

    @property
    def stages(self) -> tuple[DeploymentStageResult, ...]:
        return tuple(self._stages)

    @property
    def failed_stage(self) -> Optional[DeploymentStageResult]:
        return next(
            (stage for stage in reversed(self._stages) if not stage.succeeded),
            None,
        )

    def record(self, result: DeploymentStageResult) -> bool:
        self._stages.append(result)
        return result.succeeded

    def succeed(
        self,
        stage: DeploymentStage,
        message: str,
        *,
        host: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.record(
            DeploymentStageResult.success(
                stage, message, host=host, details=details
            )
        )

    def skip(
        self,
        stage: DeploymentStage,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.record(DeploymentStageResult.skipped(stage, message, details=details))

    def failure_response(
        self,
        stage: DeploymentStage,
        message: str,
        *,
        host: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        targets: Optional[list[str]] = None,
        head: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self.record(
            DeploymentStageResult.failure(
                stage, message, host=host, details=details
            )
        )
        return self.response(
            "error", message, targets=targets, head=head, **extra
        )

    def success_response(
        self,
        message: str,
        *,
        targets: list[str],
        head: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.response(
            "success", message, targets=targets, head=head, **extra
        )

    def response(
        self,
        status: Literal["success", "error"],
        message: str,
        *,
        targets: Optional[list[str]] = None,
        head: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        failed = self.failed_stage
        if status == "success" and failed is not None:
            raise ValueError(
                f"cannot report deployment success after failed stage "
                f"{failed.stage.value}"
            )
        if status == "error" and failed is None:
            raise ValueError("deployment error response requires a failed stage")

        result: dict[str, Any] = {"status": status, "message": message}
        if targets is not None:
            result["targets"] = targets
        if head is not None:
            result["head"] = head
        if failed is not None:
            result["failed_stage"] = failed.stage.value
        result["stages"] = [stage.as_dict() for stage in self._stages]
        result.update(extra)
        return result
