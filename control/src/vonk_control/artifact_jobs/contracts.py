"""Artifact Jobs: contracts."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NoReturn

from pydantic import ConfigDict, Field, TypeAdapter
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentProtocolError,
    ArtifactPreparation,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    RecipeJobFile,
    RecipeJobInputFile,
    RecipeJobInputRequest,
    RecipeJobOutputLimits,
    RecipeJobOutputMapping,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_agent_protocol.job_limits import (
    MAX_INPUT_FILE_BYTES,
    MAX_INPUT_FILES,
    MAX_INPUT_TOTAL_BYTES,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TOTAL_BYTES,
    MAX_TIMEOUT_SECONDS,
)
from vonk_forge_contracts import RecipeDefinition, RecipeDisplay, read_recipe

from ..artifact_blob_store import ArtifactBlobStoreError, BlobReconciliation
from ..artifact_job_evidence import (
    ArtifactJobResultEvidence,
    OutputContractBreach,
    OutputContractRule,
)
from ..compiled_artifact_contract import (
    ArtifactSlotContract,
    CompiledArtifactContract,
    ParameterDefinition,
    ParameterScalar,
    compile_artifact_contract,
)
from ..jobs.documents import RecipeJobRunParent
from ..library.contract import UuidId
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import ArtifactJob, CatalogDocument, CatalogDocumentRevision, Job, RunNode
from ..run_switch_contract import MemoryKind
from ..stored.json_values import read_row_column
from ..stored.states import CatalogRevisionState
from ..strict_json import StrictJSONModel, read_stored_model

_UUID_ID_ADAPTER = TypeAdapter(UuidId)


#: The lifecycle states of a submitted artifact job.
ArtifactJobState = Literal[
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.BACKOFF,
    LifecycleState.OBSERVING,
    LifecycleState.NEEDS_OPERATOR,
    LifecycleState.SUCCEEDED,
    LifecycleState.FAILED,
    LifecycleState.CANCELLED,
]
#: The preparation stages of a job that has not been submitted.
ArtifactPreparationStage = Literal[ArtifactPreparation.DRAFT, ArtifactPreparation.READY]


class ArtifactJobError(ValueError):
    pass


class ArtifactJobUnavailableError(UnknownOutcomeError, ArtifactJobError):
    """Stored job state cannot be read now; the lifecycle core observes and retries."""


class ArtifactJobTransferClosedError(InvalidRequestError, ArtifactJobError):
    """A request against a transfer that is already closed."""


class ArtifactJobInvalid(InvalidRequestError, ArtifactJobError):
    """A request outside the job's, the run's or the recipe's contract."""


class ArtifactOutputBreachError(ArtifactJobInvalid):
    """Outputs outside the job's output contract, with the rule they broke.

    The same outputs break it on every look, so a job whose final manifest
    does ends failed at once with ``breach`` as its evidence.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: InvalidRequestReason,
        rule: OutputContractRule,
        limit: int | None = None,
        observed: int | None = None,
        entry: str | None = None,
        slot: str | None = None,
    ) -> None:
        super().__init__(message, reason=reason)
        self.breach = OutputContractBreach(
            rule=rule, limit=limit, observed=observed, entry=entry, slot=slot
        )


class ArtifactJobRefused(SecurityRefusalError, ArtifactJobError):
    """A refusal at the job's identity, authority or content-digest edge."""


class ArtifactResultRefused(SecurityRefusalError, AgentProtocolError):
    """An agent result reported under another job's identity."""


def _translate_blob_error(error: ArtifactBlobStoreError) -> NoReturn:
    """Carry a blob-store failure into the job's error family, in its category."""

    if isinstance(error, SecurityRefusalError):
        raise ArtifactJobRefused(
            str(error),
            reason=error.typed_reason or SecurityRefusalReason.FORBIDDEN,
        ) from error
    if isinstance(error, UnknownOutcomeError):
        # Preserve the typed observation handoff through the API boundary.
        # Rewrapping it as ArtifactJobError lets broad conflict catches refuse it.
        raise error
    reason = (
        error.typed_reason
        if isinstance(error, InvalidRequestError) and error.typed_reason is not None
        else InvalidRequestReason.MALFORMED
    )
    raise ArtifactJobInvalid(str(error), reason=reason) from error


def _active_recipe_revision(
    session: Session, revision_id: str
) -> tuple[CatalogDocumentRevision, RecipeDefinition] | None:
    revision = session.get(CatalogDocumentRevision, revision_id)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != CatalogRevisionState.ACTIVE
    ):
        return None
    try:
        recipe = read_recipe(revision.document)
    except (TypeError, ValueError):
        return None
    return revision, recipe


def _recipe_display(
    session: Session, revision: CatalogDocumentRevision
) -> RecipeDisplay | None:
    """The recipe's current display (knob wording); ``None`` when it has none yet
    or cannot be read, and the contract then carries no wording."""

    document = session.get(CatalogDocument, revision.document_id)
    return None if document is None else document.readable_recipe_display()


class ArtifactJobContractModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactFileDeclaration(ArtifactJobContractModel):
    slot: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    media_type: str = Field(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
        r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
    )
    size_bytes: int = Field(ge=0, le=MAX_INPUT_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactOutputFile(ArtifactJobContractModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    media_type: str = Field(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
        r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
    )
    size_bytes: int = Field(ge=0, le=MAX_OUTPUT_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OutputLimits(ArtifactJobContractModel):
    max_files: int = Field(ge=1, le=MAX_OUTPUT_FILES)
    max_file_bytes: int = Field(ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    allowed_media_types: list[str] = Field(min_length=1, max_length=16)


def _read_input_manifest(job: ArtifactJob) -> RecipeJobInputManifest | Damaged:
    """The declared input manifest as stored, or ``Damaged`` when it does not
    parse or no longer has the identity the job was created under."""

    manifest = read_stored_model(
        RecipeJobInputManifest,
        canonical_message(job.input_manifest),
        from_json=True,
    )
    if (
        manifest.total_bytes != job.input_total_bytes
        or recipe_job_manifest_sha256(tuple(manifest.files))
        != job.input_manifest_sha256
    ):
        return Damaged("stored artifact input manifest identity is invalid")
    return manifest


class ArtifactJobResponse(ArtifactJobContractModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    run_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    submit_request_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    interface: Literal[
        "audio-job", "video-job", "image-job", "mesh-job", "artifact-job"
    ]
    #: The lifecycle state; absent (``None``) while the job is being prepared.
    state: ArtifactJobState | None
    #: ``draft`` while inputs upload, ``ready`` once complete; absent after submit.
    preparation: ArtifactPreparationStage | None = None
    #: When a cancel was requested; the state stays the core's (it completes by itself).
    cancel_requested_at: datetime | None = None
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_contract: CompiledArtifactContract | None
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_total_bytes: int = Field(ge=0)
    input_declarations: tuple[ArtifactFileDeclaration, ...] | None
    input_files: tuple[ArtifactFileDeclaration, ...]
    output_limits: OutputLimits | None
    output_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_files: tuple[ArtifactOutputFile, ...]
    result_evidence: ArtifactJobResultEvidence | None = None
    status_reason: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    created_at: datetime
    updated_at: datetime
    supported_actions: tuple[Literal["stop"], ...] = ()


class ArtifactJobListResponse(ArtifactJobContractModel):
    jobs: list[ArtifactJobResponse] = Field(max_length=100)


class ArtifactJobTransportCapabilities(ArtifactJobContractModel):
    max_input_files: int = Field(ge=1, le=MAX_INPUT_FILES)
    max_input_file_bytes: int = Field(ge=1, le=MAX_INPUT_FILE_BYTES)
    max_input_total_bytes: int = Field(ge=1, le=MAX_INPUT_TOTAL_BYTES)
    max_output_files: int = Field(ge=1, le=MAX_OUTPUT_FILES)
    max_output_file_bytes: int = Field(ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_output_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    max_timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    reserved_input_names: list[str] = Field(min_length=1, max_length=32)


class ArtifactJobStorageCapabilities(ArtifactJobContractModel):
    max_stored_bytes: int = Field(ge=1)
    used_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    in_flight_uploads: int = Field(ge=0)
    remaining_bytes: int = Field(ge=0)


class StorageReconciliation(BlobReconciliation):
    """One storage reconciliation: expired jobs and blobs, then the store's own."""

    expired_jobs: int = Field(ge=0)
    removed_blob_records: int = Field(ge=0)


class ArtifactJobCapabilitiesResponse(ArtifactJobContractModel):
    transport: ArtifactJobTransportCapabilities
    storage: ArtifactJobStorageCapabilities


@dataclass(frozen=True, slots=True)
class _JobRank:
    """One rank of the run a job fans out to, with its fabric facts."""

    node: RunNode
    fabric_address: str | None
    memory_kind: MemoryKind


@dataclass(frozen=True, slots=True)
class _JobLaunch:
    """What a job's submission needs from its stored evidence."""

    installed_plan: WireCompiledExecutionPlan
    memory_floor_bytes: int
    contract: CompiledArtifactContract
    parameters: dict[str, ParameterScalar]
    input_files: list[RecipeJobInputFile]


@dataclass(frozen=True, slots=True)
class ArtifactJobView:
    """Internal lifecycle projection; HTTP routes validate it at the boundary."""

    id: str
    run_id: str
    operation_id: str | None
    submit_request_id: str | None
    interface: str
    state: str | None
    preparation: str | None
    cancel_requested_at: datetime | None
    contract_sha256: str
    compiled_contract: CompiledArtifactContract | None
    input_manifest_sha256: str
    input_total_bytes: int
    input_declarations: tuple[ArtifactFileDeclaration, ...] | None
    input_files: tuple[ArtifactFileDeclaration, ...]
    output_limits: OutputLimits | None
    output_manifest_sha256: str | None
    output_files: tuple[ArtifactOutputFile, ...]
    result_evidence: ArtifactJobResultEvidence | None
    status_reason: str | None
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime
    #: The operator actions that apply now (``stop`` while the job is in doubt).
    supported_actions: tuple[str, ...] = ()


def _record_unservable_run(run_id: str, note: str) -> None:
    """Record why a run cannot take a job; the run itself is reconciled by the
    Run/Switch lifecycle, and the submit is refused request-led."""

    retire_as_unknown(
        "artifact-job.run",
        run_id,
        BookkeepingReason.PERSISTED_STATE_DAMAGED,
        note,
    )


def _recipe_interface(recipe: RecipeDefinition) -> str | None:
    """The one artifact interface a recipe declares; ``None`` when it declares
    none or several (the recipe then does not serve the interface asked for)."""

    artifact_interfaces = [
        item.adapter for item in recipe.interfaces if item.adapter != "openai"
    ]
    if len(artifact_interfaces) != 1:
        return None
    return artifact_interfaces[0]


def _finite_parameter_number(value: object) -> bool:
    return (isinstance(value, float) and math.isfinite(value)) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _compile_contract(
    recipe: RecipeDefinition,
    interface_name: str,
    display: RecipeDisplay | None = None,
) -> CompiledArtifactContract | Damaged:
    """The contract a recipe declares, or ``Damaged`` (with the reason) when it
    does not compile: the caller decides, a request refuses the recipe and a
    rebuild finds no evidence."""

    try:
        return compile_artifact_contract(recipe, interface_name, display)
    except (TypeError, ValueError) as error:
        return Damaged(str(error))


def _output_mappings(
    contract: CompiledArtifactContract,
) -> tuple[RecipeJobOutputMapping, ...]:
    return tuple(
        RecipeJobOutputMapping(
            slot=slot.id,
            media_type=slot.media_types[0],
            extensions=tuple(slot.extensions),
        )
        for slot in contract.output.slots
    )


def _effective_output_limits(
    parsed: CompiledArtifactContract,
    requested: RecipeJobOutputLimits,
) -> RecipeJobOutputLimits:
    try:
        allowed = RecipeJobOutputLimits.parse(
            parsed.output_limits.model_dump(mode="json")
        )
    except (AgentProtocolError, KeyError, TypeError) as error:
        raise ArtifactJobUnavailableError(
            "artifact output limit observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    if (
        requested.max_files > allowed.max_files
        or requested.max_file_bytes > allowed.max_file_bytes
        or requested.max_total_bytes > allowed.max_total_bytes
        or not set(requested.allowed_media_types) <= set(allowed.allowed_media_types)
    ):
        raise ArtifactJobInvalid(
            "artifact output limits exceed the recipe contract",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
        )
    required_slots = [item for item in parsed.output.slots if item.min_files > 0]
    if requested.max_files < sum(item.min_files for item in required_slots) or any(
        not set(item.media_types) & set(requested.allowed_media_types)
        for item in required_slots
    ):
        raise ArtifactJobInvalid(
            "artifact output limits cannot satisfy the recipe contract",
            reason=InvalidRequestReason.CONFLICT,
        )
    return requested


#: The one extension -> media type table for job inputs of every recipe. A client never names a type: the Controller derives it from the file
#: name, intersected with the slot's own declared media types.
EXTENSION_MEDIA_TYPES: Mapping[str, str] = {
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".obj": "model/obj",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
    ".ply": "model/ply",
    ".png": "image/png",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


def slot_input_media_type(slot: ArtifactSlotContract, name: str) -> str | None:
    """The slot's media type for a file name, or ``None`` when it fits none."""
    lowered = name.lower()
    declared = slot.extensions or tuple(EXTENSION_MEDIA_TYPES)
    matching = [ext for ext in declared if lowered.endswith(ext)]
    if slot.extensions and not matching:
        return None
    candidates = {
        EXTENSION_MEDIA_TYPES[ext]
        for ext in matching
        if EXTENSION_MEDIA_TYPES.get(ext) in slot.media_types
    }
    if candidates:
        return min(candidates)
    return slot.media_types[0] if len(slot.media_types) == 1 else None


def _derive_inputs(
    parsed: CompiledArtifactContract,
    requests: Sequence[RecipeJobInputRequest],
) -> tuple[RecipeJobInputFile, ...]:
    slots = {item.id: item for item in parsed.input.slots}
    derived: list[RecipeJobInputFile] = []
    for item in requests:
        slot = slots.get(item.slot)
        if slot is None:
            if not slots:
                raise ArtifactJobInvalid(
                    "recipe does not accept artifact input files",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            raise ArtifactJobInvalid(
                f"artifact input slot {item.slot} is undeclared",
                reason=InvalidRequestReason.UNKNOWN_FIELD,
            )
        media_type = slot_input_media_type(slot, item.name)
        if media_type is None:
            raise ArtifactJobInvalid(
                f"artifact input {item.name} does not fit slot {item.slot}; "
                f"it takes the extensions: {', '.join(slot.extensions)}",
                reason=InvalidRequestReason.UNSUPPORTED,
            )
        derived.append(
            RecipeJobInputFile(
                slot=item.slot,
                name=item.name,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                media_type=media_type,
            )
        )
    return tuple(derived)


def _validate_inputs_against_contract(
    parsed: CompiledArtifactContract,
    inputs: tuple[RecipeJobInputFile, ...],
) -> None:
    slots = {item.id: item for item in parsed.input.slots}
    for item in inputs:
        slot = slots[item.slot]
        if item.size_bytes > slot.max_file_bytes:
            raise ArtifactJobInvalid(
                f"artifact input {item.name} exceeds its slot limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
    for slot_id, slot in slots.items():
        selected = [item for item in inputs if item.slot == slot_id]
        if not slot.min_files <= len(selected) <= slot.max_files:
            raise ArtifactJobInvalid(
                f"artifact input slot {slot_id} file count is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if sum(item.size_bytes for item in selected) > slot.max_total_bytes:
            raise ArtifactJobInvalid(
                f"artifact input slot {slot_id} bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
    if sum(item.size_bytes for item in inputs) > parsed.input.max_bytes:
        raise ArtifactJobInvalid(
            "artifact job input bytes exceed the recipe contract",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
        )


def _validate_outputs_against_contract(
    parsed: CompiledArtifactContract,
    outputs: Sequence[RecipeJobFile],
    *,
    terminal: bool,
) -> None:
    slots = parsed.output.slots
    assignments: dict[str, list[RecipeJobFile]] = {slot.id: [] for slot in slots}
    for output in outputs:
        matches = [
            (len(extension.encode("utf-8")), slot)
            for slot in slots
            if output.media_type in slot.media_types
            for extension in slot.extensions
            if output.name.endswith(extension)
        ]
        if not matches:
            raise ArtifactOutputBreachError(
                f"artifact output {output.name} has no unique slot",
                reason=InvalidRequestReason.MALFORMED,
                rule="job_output_slot",
                entry=output.name,
            )
        longest = max(length for length, _slot in matches)
        longest_slots = {slot.id: slot for length, slot in matches if length == longest}
        if len(longest_slots) != 1:
            raise ArtifactOutputBreachError(
                f"artifact output {output.name} has no unique slot",
                reason=InvalidRequestReason.MALFORMED,
                rule="job_output_slot",
                entry=output.name,
            )
        slot = next(iter(longest_slots.values()))
        if output.size_bytes > slot.max_file_bytes:
            raise ArtifactOutputBreachError(
                f"artifact output {output.name} exceeds its slot limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
                rule="job_output_slot_max_file_bytes",
                limit=slot.max_file_bytes,
                observed=output.size_bytes,
                entry=output.name,
                slot=slot.id,
            )
        assignments[slot.id].append(output)
    for slot in slots:
        selected = assignments[slot.id]
        minimum = slot.min_files if terminal else 0
        if not minimum <= len(selected) <= slot.max_files:
            raise ArtifactOutputBreachError(
                f"artifact output slot {slot.id} file count is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
                rule="job_output_slot_files",
                limit=minimum if len(selected) < minimum else slot.max_files,
                observed=len(selected),
                slot=slot.id,
            )
        slot_bytes = sum(item.size_bytes for item in selected)
        if slot_bytes > slot.max_total_bytes:
            raise ArtifactOutputBreachError(
                f"artifact output slot {slot.id} bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
                rule="job_output_slot_max_total_bytes",
                limit=slot.max_total_bytes,
                observed=slot_bytes,
                slot=slot.id,
            )
    total_bytes = sum(item.size_bytes for item in outputs)
    if total_bytes > parsed.output.max_total_bytes:
        raise ArtifactOutputBreachError(
            "artifact output bytes exceed the recipe contract",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
            rule="job_output_contract_max_total_bytes",
            limit=parsed.output.max_total_bytes,
            observed=total_bytes,
        )


def _validate_outputs_against_limits(
    limits: RecipeJobOutputLimits, outputs: Sequence[RecipeJobFile]
) -> None:
    """The output limits the job was submitted with, over a whole manifest."""

    for output in outputs:
        if output.media_type not in limits.allowed_media_types:
            raise ArtifactOutputBreachError(
                "artifact output media type is not allowed",
                reason=InvalidRequestReason.UNSUPPORTED,
                rule="job_output_allowed_media_type",
                entry=output.name,
            )
    if len(outputs) > limits.max_files:
        raise ArtifactOutputBreachError(
            "artifact output file count exceeds the limit",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
            rule="job_output_max_files",
            limit=limits.max_files,
            observed=len(outputs),
        )
    for output in outputs:
        if output.size_bytes > limits.max_file_bytes:
            raise ArtifactOutputBreachError(
                "artifact output bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
                rule="job_output_max_file_bytes",
                limit=limits.max_file_bytes,
                observed=output.size_bytes,
                entry=output.name,
            )
    total_bytes = sum(item.size_bytes for item in outputs)
    if total_bytes > limits.max_total_bytes:
        raise ArtifactOutputBreachError(
            "artifact output bytes exceed the limit",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
            rule="job_output_max_total_bytes",
            limit=limits.max_total_bytes,
            observed=total_bytes,
        )


def _numeric_rule(definition: ParameterDefinition) -> str:
    """The range and step a numeric parameter declares, worded from the
    definition itself."""

    low, high, step = definition.minimum, definition.maximum, definition.step
    if low is not None and high is not None:
        text = f"{low}..{high}"
    else:
        text = f">= {low}" if low is not None else f"<= {high}"
    return f"{text} in steps of {step}" if step else text


def _numeric_fits(definition: ParameterDefinition, value: float) -> bool:
    low, high, step = definition.minimum, definition.maximum, definition.step
    return not (
        low is not None
        and value < low
        or high is not None
        and value > high
        or step is not None
        and low is not None
        and (value - low) % step
    )


def _effective_parameters(
    definitions: Sequence[ParameterDefinition],
    supplied: Mapping[str, ParameterScalar],
) -> dict[str, ParameterScalar]:
    by_name = {item.name: item for item in definitions}
    if set(supplied) - set(by_name):
        raise ArtifactJobInvalid(
            "artifact job contains undeclared parameters",
            reason=InvalidRequestReason.UNKNOWN_FIELD,
        )
    effective: dict[str, ParameterScalar] = {}
    for name, definition in by_name.items():
        value = supplied.get(name, definition.default)
        kind = definition.type
        valid_type = (
            kind == "string"
            and isinstance(value, str)
            and "\x00" not in value
            and len(value.encode("utf-8")) <= 4096
            or kind == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or kind == "float"
            and _finite_parameter_number(value)
            or kind == "boolean"
            and isinstance(value, bool)
            or kind == "enum"
            and any(
                type(value) is type(item) and value == item
                for item in definition.allowed_values
            )
        )
        if not valid_type:
            raise ArtifactJobInvalid(
                f"artifact job parameter {name} has the wrong type",
                reason=InvalidRequestReason.MALFORMED,
            )
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and not _numeric_fits(definition, value)
        ):
            raise ArtifactJobInvalid(
                f"artifact job parameter {name} must be "
                f"{_numeric_rule(definition)} (got {value})",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if isinstance(definition.pattern, str) and isinstance(value, str):
            try:
                matched = re.fullmatch(definition.pattern, value) is not None
            except re.error as error:
                raise ArtifactJobUnavailableError(
                    "artifact parameter pattern observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            if not matched:
                raise ArtifactJobInvalid(
                    f"artifact job parameter {name} does not match",
                    reason=InvalidRequestReason.MALFORMED,
                )
        effective[name] = value
    return effective


def _canonical_declared_parameters(
    contract: CompiledArtifactContract, value: object
) -> dict[str, ParameterScalar]:
    """Load persisted parameters as canonical JSON and enforce the contract.

    Parameter names and their scalar shapes come from the compiled recipe
    contract.  The persisted JSON object is therefore validated by the same
    declared-parameter path as a create request; this intentionally does not
    maintain a separate engine-key allowlist.
    """
    try:
        decoded = TypeAdapter(dict[str, ParameterScalar]).validate_json(
            canonical_message(value)
        )
    except (TypeError, ValueError) as error:
        raise ArtifactJobInvalid(
            "artifact job parameters must be a JSON object",
            reason=InvalidRequestReason.MALFORMED,
        ) from error
    return _effective_parameters(contract.parameters, decoded)


def _artifact_submission_in_session(
    session: Session, artifact_job: ArtifactJob
) -> Job | Residue | None:
    """The submission that owns a submitted job, ``None`` before it is submitted.

    A submission whose row, digest or request identity does not hold is damaged
    bookkeeping: it is retired as unknown and the job is shown without it.
    """

    if artifact_job.operation_id is None:
        return None

    def read() -> Job | Damaged:
        submission = session.get(Job, artifact_job.operation_id)
        if submission is None or submission.kind != "recipe.job.run.v1":
            return Damaged("artifact job submission owner is invalid")
        payload = read_row_column(submission, "payload")
        if (
            not isinstance(payload, RecipeJobRunParent)
            or payload.owner_kind != "artifact-job"
            or payload.owner_id != artifact_job.id
            or hashlib.sha256(canonical_message(payload)).hexdigest()
            != submission.payload_digest
        ):
            return Damaged("artifact job submission owner is invalid")
        _UUID_ID_ADAPTER.validate_python(submission.request_id, strict=True)
        return submission

    return read_or_rebuild(
        kind="artifact-job.submission", subject=artifact_job.id, read=read
    )
