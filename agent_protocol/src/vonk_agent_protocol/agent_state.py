"""Records the Rust agent and its privileged helper persist beside their work.

These are not Controller messages: each is a small JSON document one Spark
process writes and a later process (often after a restart or a package upgrade)
reads back.  They are data classes all the same, so the Pydantic model here is
the one definition and ``scripts/generate-agent-wire`` generates the Rust type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from .agent_words import HostHelperResponseStatus
from .claims import AgentRuntimeIdentity
from .compiled_execution_plan import CompiledPlacement
from .helper_response import HostHelperProcessLogs
from .host_helper import RecipeReconciliationIdentity, Uuid4Text
from .package_upgrade import PackageActivationPhase, PackageRollbackAuthority
from .wire_model import MAX_RUN_GENERATION, Digest, WireModel, strict_json_datetime

U32 = Annotated[int, Field(ge=0, le=2**32 - 1)]
U64 = Annotated[int, Field(ge=0, le=2**64 - 1)]
I64 = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
# A daemon image id or manifest digest as the container runtime spells it.
OciDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class AgentGenerationPointer(WireModel):
    """The active identity generation, switched last when an identity rotates."""

    generation: U64


class AgentIdentityMetadata(WireModel):
    """The certificate facts persisted beside an agent identity's key."""

    fingerprint: str
    generation: U64
    node_id: str
    serial: str


class AgentReadinessReceipt(WireModel):
    """The Controller-accepted proof that this exact agent process is running."""

    schema_version: Literal[1]
    pid: U32
    process_start_ticks: U64
    boot_id: str
    accepted_at: datetime
    runtime_identity: AgentRuntimeIdentity

    @field_validator("accepted_at", mode="before")
    @classmethod
    def parse_accepted_at(cls, value: object) -> object:
        return strict_json_datetime(value)


class InstallationReconciliationCheckpoint(WireModel):
    """Where a recipe installation's reconciliation stopped, for crash recovery."""

    schema_version: Literal[3]
    state: Literal["prepared", "removing", "complete"]
    identity: RecipeReconciliationIdentity
    installation_device: U64
    installation_inode: U64


class InstallationMetadataEntry(WireModel):
    """One file of an installation, as it was verified."""

    selection_id: str
    path: str
    sha256: Digest
    size_bytes: U64
    dev: U64
    ino: U64
    mtime_ns: I64
    ctime_ns: I64


class InstallationMetadataReceipt(WireModel):
    """The verified file set of one installed model."""

    schema_version: Literal[2]
    entries: list[InstallationMetadataEntry]


class RunLifecycleRecord(WireModel):
    """The lifecycle a running recipe was started with.

    Tolerant of fields an older or newer agent wrote: an unknown field must not
    make a retained run unreadable.
    """

    model_config = ConfigDict(
        extra="ignore", strict=True, frozen=True, allow_inf_nan=False
    )

    installation_id: str
    placement: CompiledPlacement
    run_generation: Annotated[int, Field(ge=0, le=MAX_RUN_GENERATION)] | None = None


class HostOperationOutcome(WireModel):
    """The privileged helper's durable result for one operation."""

    schema_version: Literal[1]
    status: HostHelperResponseStatus
    exit_code: I64 | None = None
    # A one-shot job that did not exit cleanly: its exit account and the
    # container's own bounded output, captured before the helper removed it.
    diagnostic: Annotated[str, Field(max_length=8192)] | None = None
    process_logs: HostHelperProcessLogs | None = None


class HostRuntimeImageReceipt(WireModel):
    """The helper's proof that one pinned image was pulled to one local reference.

    The manifest digest identifies the image in the Controller's layered store;
    the daemon image id identifies the pulled object, so later uses compare it
    live.
    """

    schema_version: Literal[3]
    platform_manifest_digest: OciDigest
    image_config_id: OciDigest
    local_image_reference: str


class HostArchiveIdentity(WireModel):
    """The inode a loaded image archive had when it was received."""

    device: U64
    inode: U64
    bytes: U64
    modified_seconds: I64
    modified_nanoseconds: I64
    changed_seconds: I64
    changed_nanoseconds: I64


class HostArchiveRuntimeImageReceipt(WireModel):
    """The receipt an agent before the layered image store wrote for an archive.

    An installation that names it keeps starting until it is replaced; the image
    itself is still compared live.
    """

    model_config = ConfigDict(
        extra="ignore", strict=True, frozen=True, allow_inf_nan=False
    )

    schema_version: Literal[2]
    registry_index_digest: OciDigest
    platform_manifest_digest: OciDigest
    archive_sha256: Digest
    archive_bytes: U64
    archive_identity: HostArchiveIdentity
    archive_config_id: OciDigest
    image_config_id: OciDigest
    local_image_reference: str


class InstallationReconciliationReceipt(WireModel):
    """The helper's record of the installation a reconciliation removes."""

    schema_version: Literal[3]
    identity: RecipeReconciliationIdentity
    installation_device: U64
    installation_inode: U64


class RuntimeGenerationFence(WireModel):
    """The highest run generation admitted for one runtime, and whether it ended."""

    schema_version: Literal[2]
    installation_id: Uuid4Text
    runtime_id: Uuid4Text
    highest_generation: Annotated[int, Field(ge=0, le=MAX_RUN_GENERATION)]
    cancelled: bool


class PackageRollbackTransaction(WireModel):
    """The root-custody package activation transaction, restored after a crash."""

    schema_version: Literal[2]
    node_id: Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
    candidate_sha256: Digest
    candidate_version: str
    candidate_binary_sha256: Digest
    candidate_helper_sha256: Digest
    rollback: PackageRollbackAuthority
    phase: PackageActivationPhase
    created_at: I64
    updated_at: I64
    outcome: str
