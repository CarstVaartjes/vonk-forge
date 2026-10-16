"""Resource planning: types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, get_args

from vonk_forge_contracts.recipe import (
    RecipeDiskResources,
    Scalar,
)

from ..run_switch_contract import (
    MemoryKind,
    RunSwitchChangeEffect,
)
from .identity import _canonical
from .memory_kinds import memory_reservation_kind

if TYPE_CHECKING:
    from ..owner_liveness import OwnerRef

EvidenceState = Literal["declared", "measured", "fresh", "stale", "unknown"]

Effect = Literal["reuse", "restart", "reprepare", "reinstall", "rebuild"]

_CHANGE_EFFECTS: frozenset[RunSwitchChangeEffect] = frozenset(
    get_args(RunSwitchChangeEffect)
)

# The memory the platform keeps free on every Spark beyond a workload's declared
# peak. A recipe's own ``reserve_bytes`` is informational and is never added.
PLATFORM_MEMORY_FLOOR_BYTES = 6_000_000_000


@dataclass(frozen=True, slots=True)
class ResourceReason:
    code: str
    detail: str
    severity: Literal["blocker", "warning"] = "blocker"
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstallationDiskRequirement:
    required_bytes: int
    floor_bytes: int


def installation_disk_requirement(
    disk: RecipeDiskResources,
    *,
    required_download_bytes: int,
    minimum_floor_bytes: int,
) -> InstallationDiskRequirement:
    """One disk envelope for operator review and installation admission.

    The caller supplies exact missing payload bytes, or a full allocation when
    it cannot yet promise reuse. The reserve remains separate from consumed
    bytes so installation does not mistake headroom for downloaded data.
    """
    if required_download_bytes < 0 or minimum_floor_bytes < 0:
        raise ValueError("installation disk envelope contains negative bytes")
    return InstallationDiskRequirement(
        required_download_bytes + disk.working_bytes,
        max(minimum_floor_bytes, disk.safety_margin_bytes),
    )


@dataclass(frozen=True, slots=True)
class ParallelismSettings:
    world_size: int
    tensor: int
    pipeline: int
    data: int
    backend: str


@dataclass(frozen=True, slots=True)
class EffectiveResourceSettings:
    kind: Literal["generation", "embedding", "job"]
    context_tokens: int | None
    concurrency: int | None
    batch_tokens: int | None
    parallelism: ParallelismSettings
    knobs: Mapping[str, Scalar] = field(default_factory=dict)
    change_effects: Mapping[str, RunSwitchChangeEffect] = field(default_factory=dict)
    identity_digest: str = ""

    def identity(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "context_tokens": self.context_tokens,
            "concurrency": self.concurrency,
            "batch_tokens": self.batch_tokens,
            "parallelism": {
                "world_size": self.parallelism.world_size,
                "tensor": self.parallelism.tensor,
                "pipeline": self.parallelism.pipeline,
                "data": self.parallelism.data,
                "backend": self.parallelism.backend,
            },
            "knobs": _canonical(self.knobs),
        }


@dataclass(frozen=True, slots=True)
class SettingsResolution:
    settings: EffectiveResourceSettings | None
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.settings is not None and not any(
            reason.severity == "blocker" for reason in self.reasons
        )


@dataclass(frozen=True, slots=True)
class ResourceEvidence:
    weights_bytes: int | None
    runtime_overhead_bytes: int | None
    baseline_context_tokens: int | None = None
    baseline_concurrency: int | None = None
    baseline_batch_tokens: int | None = None
    context_bytes_per_token: int | None = None
    concurrency_bytes_per_request: int | None = None
    batch_bytes_per_token: int | None = None
    supported_context_tokens: tuple[int, int] | None = None
    supported_concurrency: tuple[int, int] | None = None
    supported_batch_tokens: tuple[int, int] | None = None
    evidence_state: EvidenceState = "unknown"
    evidence_digest: str | None = None
    declared_total_bytes: int | None = None
    #: The highest attributed use of earlier runs of this exact recipe content
    #: on this memory kind. It only ever raises the declared bound.
    observed_total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceDemand:
    weights_bytes: int | None
    runtime_overhead_bytes: int | None
    context_bytes: int | None
    concurrency_bytes: int | None
    batch_bytes: int | None
    total_bytes: int | None
    evidence_state: EvidenceState
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.total_bytes is not None and not any(
            reason.severity == "blocker" for reason in self.reasons
        )


@dataclass(frozen=True, slots=True)
class UnknownRunMemoryResidual:
    """Upper bound for an active run whose resident usage is not observed."""

    run_id: str
    run_generation: int
    reservation_kind: str
    maximum_bytes: int


@dataclass(frozen=True, slots=True)
class MemoryClaimHolder:
    """A claim charged on top of observed free memory, and whom it is held for."""

    owner: OwnerRef
    amount_bytes: int


@dataclass(frozen=True, slots=True)
class MemoryReservationTotals:
    """Hard peak commitments and bounds not resolved by physical observations."""

    committed_bytes_by_kind: Mapping[str, int]
    unmaterialized_bytes_by_kind: Mapping[str, int]
    unknown_run_residuals_by_kind: Mapping[
        str, tuple[UnknownRunMemoryResidual, ...]
    ] = field(default_factory=dict)
    #: Claims of runs whose Stop receipts the Controller confirmed after the
    #: inventory sample was taken. The sample still counts those runs' memory as
    #: used, so the freed bytes are credited back, never more than the sample
    #: reports as occupied.
    released_unobserved_bytes_by_kind: Mapping[str, int] = field(default_factory=dict)
    #: Claims of runs the plan keeps. The sample already counts what they hold,
    #: so they are not charged; they are named only because they hold memory.
    kept_bytes_by_kind: Mapping[str, int] = field(default_factory=dict)
    #: The not-yet-materialized claims, by reservation kind: what a shortfall
    #: against observed free memory waits on.
    unmaterialized_holders_by_kind: Mapping[str, tuple[MemoryClaimHolder, ...]] = field(
        default_factory=dict
    )
    #: Claims of runs the plan stops: left out of the fit, but their runs hold
    #: memory until the stop releases it.
    releasing_bytes_by_kind: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRequirement:
    """One recipe rank's demand and reserve, shared by review and admission."""

    kind: MemoryKind
    floor_bytes: int
    demand: ResourceDemand

    @property
    def reservation_kind(self) -> str:
        return memory_reservation_kind(self.kind)


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    node_id: str
    memory_kind: str
    available_bytes: int | None
    occupied_bytes: int | None
    reserved_bytes: int | None
    evidence_state: EvidenceState = "unknown"
    evidence_digest: str | None = None
    components: tuple[CapacitySnapshot, ...] = ()
    unmaterialized_bytes: int | None = 0
    unknown_run_residuals: tuple[UnknownRunMemoryResidual, ...] = ()
    evidence_observed_at: datetime | None = None
    #: Declared bytes of the runs the plan keeps here (not charged).
    kept_bytes: int = 0
    #: The claims behind ``unmaterialized_bytes``.
    unmaterialized_holders: tuple[MemoryClaimHolder, ...] = ()
    #: Claims of runs the plan stops here (left out of ``reserved_bytes``).
    releasing_bytes: int = 0


@dataclass(frozen=True, slots=True)
class PlannedStopRelease:
    run_id: str
    node_id: str
    memory_kind: str
    release_bytes: int | None
    planned: bool
    plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class NodeCapacityPlan:
    node_id: str
    required_bytes: int | None
    current_free_after_bytes: int | None
    after_stop_free_after_bytes: int | None
    selected_free_after_bytes: int | None
    stop_required: bool
    allowed: bool
    reasons: tuple[ResourceReason, ...] = ()
    insufficient_components: tuple[str, ...] = ()
    unknown_run_residuals: tuple[UnknownRunMemoryResidual, ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    nodes: tuple[NodeCapacityPlan, ...]
    allowed: bool
    stop_before_prepare: bool
    reasons: tuple[ResourceReason, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourcePreflightPlan:
    settings: EffectiveResourceSettings | None
    demands: Mapping[str, ResourceDemand]
    capacity: CapacityPlan | None
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return (
            self.settings is not None
            and self.capacity is not None
            and self.capacity.allowed
            and not any(reason.severity == "blocker" for reason in self.reasons)
        )


@dataclass(frozen=True, slots=True)
class PreparationDecision:
    effect: Effect
    settings_changed: tuple[str, ...]
    compatibility_changed: bool
    requires_restart: bool
    requires_reprepare: bool
    requires_reinstall: bool
    requires_rebuild: bool
    settings_digest: str
