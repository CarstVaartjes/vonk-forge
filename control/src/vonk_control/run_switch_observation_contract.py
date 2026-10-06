"""Typed partial observations crossing Run/Switch executor boundaries."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter
from vonk_agent_protocol import OperationProgress

from .recipe_image_availability_clocks_contract import readable_or_none
from .recipe_lifecycle_contract import LifecycleNodeResult
from .run_switch_contract import RunSwitchMemberReceipt
from .runtime_image_preparation import RuntimeImageReceipt


class RunSwitchObservedEvidence(BaseModel):
    """Known progress and failure evidence supplied by an executor.

    The complete receipt remains validated by its producer's canonical result
    union. This read projection consumes only the shared measured evidence.
    """

    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    operation: OperationProgress | None = None
    progress: RunSwitchObservedEvidence | None = None
    completed_bytes: int | None = Field(default=None, ge=0)
    copied_bytes: int | None = Field(default=None, ge=0)
    downloaded_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    members: list[RunSwitchMemberReceipt] = Field(default_factory=list)
    node_evidence: dict[str, LifecycleNodeResult] | None = None
    launch_evidence: dict[str, LifecycleNodeResult] | None = None
    failure_kind: str | None = None
    error_code: str | None = None
    uncertain: bool = False
    child_state: str | None = None
    status_reason: str | None = None
    reason: str | None = None


class RunSwitchEffectiveBuildReceipt(BaseModel):
    """The exact executable build output selected for a later phase."""

    model_config = ConfigDict(extra="forbid", strict=True)
    build_id: str | None
    build_input_sha256: (
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None
    )
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    oci_layout_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    image_bytes: int = Field(gt=0)


class RunSwitchStoredIdentity(BaseModel):
    """Readable identity retained independently of damaged phase bookkeeping.

    This projection never authorizes a new effect.
    """

    model_config = ConfigDict(extra="ignore", strict=True)
    action: Annotated[
        Literal["install", "run", "stop", "switch", "cleanup"] | None,
        readable_or_none(
            TypeAdapter(Literal["install", "run", "stop", "switch", "cleanup"])
        ),
    ] = None
    plan_digest: Annotated[
        str | None,
        readable_or_none(
            TypeAdapter(Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")])
        ),
    ] = None
    workload_intent_ordinal: Annotated[
        int | None,
        readable_or_none(TypeAdapter(Annotated[int, Field(ge=1)])),
    ] = None


DigestValue = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ObservedBytes = Annotated[
    int | None, readable_or_none(TypeAdapter(Annotated[int, Field(ge=0)]))
]
ObservedDigests = Annotated[
    list[DigestValue] | None, readable_or_none(TypeAdapter(list[DigestValue]))
]


class RunSwitchArtifactGuardEvidence(BaseModel):
    """Read projection for precise rejection of incomplete executor evidence.

    Complete phase receipts are validated separately. Unreadable fields remain
    unknown so the guard reports the original receipt-specific retry reason.
    """

    model_config = ConfigDict(extra="ignore", strict=True)
    runtime_image: Annotated[
        RuntimeImageReceipt | None, readable_or_none(TypeAdapter(RuntimeImageReceipt))
    ] = None
    artifact_set_sha256: Annotated[
        DigestValue | None, readable_or_none(TypeAdapter(DigestValue))
    ] = None
    coverage: str | None = None
    downloaded_bytes: ObservedBytes = None
    copied_bytes: ObservedBytes = None
    total_bytes: ObservedBytes = None
    verified_build_id: str | None = None
    scope: str | None = None
    nas_evicted: bool | None = None
    reclaimed_bytes: ObservedBytes = None
    protected_referenced_bytes: ObservedBytes = None
    reclaimed_digests: ObservedDigests = None
    protected_digests: ObservedDigests = None


class RunSwitchObservedImageIdentity(BaseModel):
    """Fields actually observed for an accepted Fleet profile image."""

    model_config = ConfigDict(extra="forbid", strict=True)
    build_id: str | None = None
    image_digest: str | None = None
    oci_layout_sha256: str | None = None
    image_bytes: int | None = None
    architecture: str | None = None
    runtime_interface: str | None = None
