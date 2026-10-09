"""Run switch contract: phase results."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    DistributionCode,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase

from ..distribution_assignment import NodeDistributionAssignment
from ..model_cache_contract import ModelCacheDownloadResult
from ..run_switch_identity_contract import NodeId, UuidId
from ..runtime_image_preparation import RuntimeImageReceipt
from ..strict_json import StrictModel
from .progress import (
    ArtifactVerificationEvidence,
    RunSwitchChildProgress,
    RunSwitchMemberReceipt,
    RunSwitchRankReceipt,
    _FailureText,
)
from .vocabulary import (
    Digest,
    RunSwitchContainerBuildState,
    RunSwitchPhaseKind,
    RunSwitchSubphase,
)


class ArtifactVerificationResult(StrictModel):
    """Canonical evidence returned by a completed artifact verify phase."""

    skipped: bool = False
    verified: Literal[True]
    verified_digests: list[Digest] = Field(max_length=256)
    verified_build_id: UuidId | None
    verified_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    verified_oci_layout_sha256: Digest
    cached_nodes: list[NodeId] = Field(default_factory=list, max_length=32)
    cached_target_totals: dict[NodeId, int] = Field(default_factory=dict)
    evidence: list[ArtifactVerificationEvidence] = Field(default_factory=list)


class _RunSwitchPhaseBase(StrictModel):
    # `subphase` is declared by each concrete result rather than here. A default
    # on this base makes every subclass that narrows the field to its own
    # Literal an override that drops the default, which pyright reports nine
    # times; a base field without a default instead makes it required on the
    # four results that do not narrow it, changing the committed OpenAPI. Each
    # result declaring the field it actually publishes keeps both quiet without
    # touching a published schema.
    phase: RunSwitchPhaseKind


class RunSwitchContainerBuildResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["container-build"]
    build_id: UuidId
    build_input_sha256: Digest
    state: RunSwitchContainerBuildState
    image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    oci_layout_sha256: Digest | None = None
    image_bytes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def succeeded_build_has_receipt(self) -> RunSwitchContainerBuildResult:
        if self.state == "succeeded" and (
            self.image_digest is None
            or self.oci_layout_sha256 is None
            or self.image_bytes is None
        ):
            raise ValueError("succeeded build phase requires complete image receipt")
        return self


class RunSwitchRuntimeImageResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-image"]
    runtime_image: RuntimeImageReceipt
    effective_execution_key: Digest | None = None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=1)
    build_id: UuidId | None = None

    @model_validator(mode="after")
    def outer_identity_matches_receipt(self) -> RunSwitchRuntimeImageResult:
        if (
            self.runtime_image.image_digest != self.image_digest
            or self.runtime_image.oci_archive_sha256 != self.oci_layout_sha256
            or self.runtime_image.image_bytes != self.image_bytes
        ):
            raise ValueError(
                "runtime image phase identity differs from canonical receipt"
            )
        return self


class RunSwitchModelDownloadResult(ModelCacheDownloadResult):
    phase: Literal["transfer"]
    subphase: Literal["model-download"]
    skipped: Literal[True] = True
    downloaded_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    progress: RunSwitchChildProgress
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    evidence: ModelCacheDownloadResult | None = None

    @model_validator(mode="after")
    def nested_identity_matches_result(self) -> RunSwitchModelDownloadResult:
        if (
            self.evidence is not None
            and self.evidence.artifact_set_sha256 != self.artifact_set_sha256
        ):
            raise ValueError("model download evidence artifact set differs from result")
        return self


class RunSwitchModelDownloadPendingResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["model-download"]
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    downloaded_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    progress: RunSwitchChildProgress
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchTargetTransferResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    cached_nodes: list[NodeId] = Field(default_factory=list, max_length=32)
    assignments: dict[NodeId, NodeDistributionAssignment] = Field(min_length=1)


class RunSwitchTargetTransferEvidenceResult(ArtifactVerificationEvidence):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]


class RunSwitchCachedTransferResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    skipped: Literal[True]
    verified: Literal[False]
    verified_digests: list[Digest] = Field(max_length=256)
    verified_build_id: UuidId | None
    verified_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    verified_oci_layout_sha256: Digest
    cached_nodes: list[NodeId] = Field(min_length=1, max_length=32)
    cached_target_totals: dict[NodeId, int]


class RunSwitchVerifyResult(ArtifactVerificationResult):
    phase: Literal["verify"]
    subphase: Literal["target-copy"]


class RunSwitchCleanupResult(_RunSwitchPhaseBase):
    phase: Literal["cleanup"]
    subphase: RunSwitchSubphase | None = None
    scope: Literal["spark-local"]
    reclaimed_bytes: int = Field(ge=0)
    protected_referenced_bytes: int = Field(default=0, ge=0)
    reclaimed_digests: list[Digest] = Field(default_factory=list, max_length=256)
    protected_digests: list[Digest] = Field(default_factory=list, max_length=256)
    nas_evicted: bool


class RunSwitchRuntimePlanResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-plan"]
    installation_id: UuidId
    mapping_id: UuidId
    install_plan_digest: Digest
    model_artifact_set_sha256: Digest | None = None
    model_artifact_set_bytes: int | None = Field(default=None, ge=0)
    compiled_plan_persisted: Literal[True]


class RunSwitchPreparedResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-plan"]
    prepared: Literal[True]


class RunSwitchRuntimeInstallResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-install"]
    installation_id: UuidId


class RunSwitchStopResult(_RunSwitchPhaseBase):
    phase: Literal["stop"]
    subphase: RunSwitchSubphase | None = None
    run_id: UuidId


class RunSwitchStartResult(_RunSwitchPhaseBase):
    phase: Literal["start"]
    subphase: RunSwitchSubphase | None = None
    run_id: UuidId


class RunSwitchUninstallResult(_RunSwitchPhaseBase):
    """The removal of one installation that is no longer desired."""

    phase: Literal["uninstall"]
    subphase: RunSwitchSubphase | None = None
    installation_id: UuidId
    # ``abandoned`` records a persisted plan that never reached a node, so the
    # operator sees why the record was disposed of without node work.
    disposition: Literal["uninstalled", "abandoned"] = "uninstalled"
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchCleanupVerifyResult(_RunSwitchPhaseBase):
    """Observed removal of the installation, derived from durable state."""

    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    installation_id: UuidId
    removed: bool
    cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall"
    active_runs: int = Field(default=0, ge=0)
    installation_state: (
        Annotated[str, StringConstraints(min_length=1, max_length=24)] | None
    ) = None
    reconciliation_request_id: UuidId | None = None

    @model_validator(mode="after")
    def reconciliation_matches_mode(self) -> RunSwitchCleanupVerifyResult:
        if (self.cleanup_mode == "reconcile") != (
            self.reconciliation_request_id is not None
        ):
            raise ValueError("only a reconciliation cleanup names its request")
        return self


class RunSwitchFinalVerifyResult(_RunSwitchPhaseBase):
    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    run_id: UuidId
    state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    route_state: Annotated[str, StringConstraints(max_length=64)]
    healthy: bool
    ranks: list[RunSwitchRankReceipt] = Field(max_length=32)


class RunSwitchInstallationVerifyResult(_RunSwitchPhaseBase):
    """Exact installed membership observed without a serving workload."""

    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    installation_id: UuidId
    installation_state: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    active_runs: int = Field(ge=0)
    unwithdrawn_routes: int = Field(ge=0)
    ranks: list[RunSwitchRankReceipt] = Field(min_length=1, max_length=32)


class RunSwitchDistributionChildResult(StrictModel):
    """Durable projection of one target-copy child operation.

    A child Job has a different persisted shape from a parent phase receipt:
    it owns member progress and per-node handoff evidence.  Keeping that
    projection separate prevents a progress snapshot from being accepted as
    a completed phase result.
    """

    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    progress: RunSwitchChildProgress
    members: list[RunSwitchMemberReceipt] = Field(min_length=1, max_length=32)
    evidence: list[ArtifactVerificationEvidence] = Field(max_length=32)
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    # Incomplete or mixed peer evidence is observed under the parent deadline.
    uncertain: bool = False
    # Exact authority denials remain distinct from unavailable bookkeeping.
    failure_kind: _FailureText | None = None
    error_code: _FailureText | None = None


class RunSwitchDistributionEndedResult(_RunSwitchPhaseBase):
    """A missing durable distribution child; no node effect is asserted."""

    phase: Literal[ProfileChildPhase.TRANSFER]
    subphase: Literal[ProfileChildPhase.TARGET_COPY]
    reason: WaitReason
    error_code: DistributionCode
    uncertain: bool = True


RunSwitchPhaseResult = (
    RunSwitchDistributionEndedResult
    | RunSwitchContainerBuildResult
    | RunSwitchRuntimeImageResult
    | RunSwitchModelDownloadResult
    | RunSwitchModelDownloadPendingResult
    | RunSwitchTargetTransferResult
    | RunSwitchCachedTransferResult
    | RunSwitchTargetTransferEvidenceResult
    | RunSwitchVerifyResult
    | RunSwitchCleanupResult
    | RunSwitchRuntimePlanResult
    | RunSwitchPreparedResult
    | RunSwitchRuntimeInstallResult
    | RunSwitchStopResult
    | RunSwitchStartResult
    | RunSwitchUninstallResult
    | RunSwitchFinalVerifyResult
    | RunSwitchCleanupVerifyResult
    | RunSwitchInstallationVerifyResult
)
