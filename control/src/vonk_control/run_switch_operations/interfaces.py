"""Interfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Protocol,
    runtime_checkable,
)

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ResourceBlockerCode,
    RunSwitchCode,
)

from ..lifecycle_preflight import LifecyclePreflightCheckpoint
from ..models import (
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from ..recipe_operations import (
    RecipeOperationView,
)
from ..run_switch_contract import (
    ConditionalPostStopMemoryCheck,
    FreshnessEvidence,
    RunSwitchCoverage,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPhaseResult,
    RunSwitchPlan,
    RunSwitchReason,
    SparkFit,
)

if TYPE_CHECKING:
    from ..distribution_executor import _ChildView


@dataclass(frozen=True, slots=True)
class ArtifactInspection:
    """Evidence returned by the cache boundary for one high-level plan."""

    required_bytes: int | None
    reused_bytes: int
    copied_bytes: int
    missing_nas_bytes: int | None
    missing_spark_bytes: int | None
    reclaimable_bytes: int
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    artifact_digests: tuple[str, ...] = ()
    reclaimable_digests: tuple[str, ...] = ()
    freshness: tuple[FreshnessEvidence, ...] = ()
    blockers: tuple[RunSwitchReason, ...] = ()
    warnings: tuple[RunSwitchReason, ...] = ()
    # Node-keyed breakdown of ``missing_spark_bytes`` (never total / N).
    missing_spark_bytes_by_node: Mapping[str, int] | None = None
    # A cache provider may expose the authoritative full manifest identity.
    # The Controller must never infer it from whichever files happen to be
    # present on one target.
    artifact_set_sha256: str | None = None
    artifact_set_bytes: int | None = None
    dependency_model_content_sha256: tuple[str, ...] = ()


class RunSwitchArtifactInspector(Protocol):
    """Read cache coverage without transferring or mutating any bytes."""

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection: ...


@dataclass(frozen=True, slots=True)
class PhaseExecution:
    """Result of one phase invocation.

    A phase can complete in the Controller transaction (``operation_id`` is
    ``None``) or hand off to an existing durable low-level operation.  The
    high-level job remains the only operation exposed to clients.
    """

    operation_id: str | None = None
    result: RunSwitchPhaseResult | None = None
    waiting: bool = False
    status_reason: str | None = None


class RunSwitchArtifactPhaseExecutor(Protocol):
    """Injected cache boundary for transfer, verify, and Spark-local cleanup.

    Implementations may return a synchronous evidence result or a durable
    child operation.  They own NAS/cache transport and destination digest
    verification; this Controller service never downloads payload bytes.
    Cleanup implementations must be Spark-local and must not evict NAS data.
    """

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution: ...

    def get(
        self, operation_id: str
    ) -> RecipeOperationView | _ChildView | RunSwitchOperation: ...


class RunSwitchPhaseExecutor(Protocol):
    """Execute a planned phase by composing existing lifecycle primitives."""

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution: ...


@runtime_checkable
class _PhasePreflightGate(Protocol):  # noqa: PYI046 -- shared executor capability
    """Optional executor capability consulted before a phase is executed."""

    def __call__(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> tuple[LifecyclePreflightCheckpoint | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class _ConflictRun:
    run: RecipeRun
    nodes: tuple[RunNode, ...]
    reserved_bytes: int
    stop_plan_digest: str | None


@dataclass(frozen=True, slots=True)
class _BuildSelection:
    """The exact build receipt or pending Controller build selected for a plan."""

    build: RecipeBuild | None
    candidate: RecipeBuild | None
    builder_freshness: FreshnessEvidence | None = None
    blockers: tuple[RunSwitchReason, ...] = ()


@dataclass(frozen=True, slots=True)
class _ResourceFits:
    """One current/after-stop resource decision for review and admission."""

    freshness: list[FreshnessEvidence]
    current: SparkFit
    after_stop: SparkFit | None
    current_blockers: list[RunSwitchReason]
    blockers: list[RunSwitchReason]
    warnings: list[RunSwitchReason]
    post_stop_memory_check: ConditionalPostStopMemoryCheck | None

    def requires_early_stop(self, *reason_prefixes: str) -> bool:
        return (
            not self.current.allowed
            and self.after_stop is not None
            and self.after_stop.allowed
            and any(
                reason.code.startswith(reason_prefixes)
                for reason in self.current_blockers
            )
        )

    @property
    def stop_before_prepare(self) -> bool:
        return (
            self.requires_early_stop(
                RunSwitchCode.INSUFFICIENT_MEMORY,
                f"run-switch.{ResourceBlockerCode.INSUFFICIENT}",
            )
            or self.post_stop_memory_check is not None
        )

    @property
    def stop_before_transfer(self) -> bool:
        return self.requires_early_stop(RunSwitchCode.INSUFFICIENT_DISK)
