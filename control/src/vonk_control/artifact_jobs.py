"""Durable, bounded, content-addressed artifact-producing recipe jobs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentProtocolError,
    ArtifactPreparation,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    LifecycleSubject,
    RecipeJobFile,
    RecipeJobInputFile,
    RecipeJobOutputLimits,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    RunState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
    recipe_job_manifest_sha256,
    state_adopter,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_forge_contracts import RecipeDefinition, read_recipe

from . import agent_operation_states
from . import artifact_job_states as ajs
from .artifact_blob_store import (
    ArtifactBlobStore,
    ArtifactBlobStoreError,
    StoredArtifactBlob,
)
from .categorized_errors import InvalidValue, MissingRecord
from .cluster_mappings import mapping_option_choices
from .compiled_artifact_contract import (
    CompiledArtifactContract,
    compile_artifact_contract,
)
from .execution_plan_service import compile_job_invocation
from .library_contract import UuidId
from .lifecycle import CancelRequested, Outcome, Reported
from .lifecycle.agent_operation import AgentOperationAdapter
from .lifecycle.artifact_job import ArtifactJobAdapter
from .lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from .models import (
    AgentOperation,
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
    parse_stored_run_plan,
)
from .recipe_operations import RecipeOperationConflict, RecipeOperationService
from .strict_json import StrictJSONModel, read_stored_model

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
_UUID_ID_ADAPTER = TypeAdapter(UuidId)


#: The lifecycle states of a submitted artifact job (an old spelling is adopted).
ArtifactJobState = Annotated[
    Literal[
        LifecycleState.QUEUED,
        LifecycleState.RUNNING,
        LifecycleState.BACKOFF,
        LifecycleState.OBSERVING,
        LifecycleState.NEEDS_OPERATOR,
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    ],
    BeforeValidator(state_adopter(LifecycleSubject.ARTIFACT_JOB)),
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


class ArtifactJobRefused(SecurityRefusalError, ArtifactJobError):
    """A refusal at the job's identity, authority or content-digest edge."""


class ArtifactResultInvalid(InvalidRequestError, AgentProtocolError):
    """An agent result that breaks the job's contract (the job ends failed)."""


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
        raise ArtifactJobUnavailableError(
            str(error),
            reason=error.typed_reason or WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
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
        or revision.state != "active"
    ):
        return None
    try:
        recipe = read_recipe(revision.document)
    except (TypeError, ValueError):
        return None
    return revision, recipe


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
    size_bytes: int = Field(ge=0, le=1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OutputLimits(ArtifactJobContractModel):
    max_files: int = Field(ge=1, le=32)
    max_file_bytes: int = Field(ge=1, le=1024**3)
    max_total_bytes: int = Field(ge=1, le=2 * 1024**3)
    allowed_media_types: list[str] = Field(min_length=1, max_length=16)


class ArtifactJobResultEvidence(ArtifactJobContractModel):
    """Known evidence fields with room for engine-specific evidence keys."""

    model_config = ConfigDict(extra="allow", strict=True)

    elapsed_milliseconds: int | None = Field(default=None, ge=0)
    peak_memory_bytes: int | None = Field(default=None, ge=0)


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


def _result_evidence(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        evidence = read_stored_model(
            ArtifactJobResultEvidence, canonical_message(value), from_json=True
        )
    except (TypeError, ValueError) as error:
        # Damaged result evidence is no evidence: nothing re-derives it, so it is
        # retired as unknown and the job is shown without it.
        retire_as_unknown(
            "artifact-job.result-evidence",
            "stored-evidence",
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"{type(error).__name__}: {error}",
        )
        return None
    return json.loads(canonical_message(evidence))


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
    compiled_contract: CompiledArtifactContract
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_total_bytes: int = Field(ge=0)
    input_declarations: tuple[ArtifactFileDeclaration, ...]
    input_files: tuple[ArtifactFileDeclaration, ...]
    output_limits: OutputLimits
    output_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_files: tuple[ArtifactOutputFile, ...]
    result_evidence: ArtifactJobResultEvidence | None = None
    status_reason: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(ge=1, le=3_600)
    created_at: datetime
    updated_at: datetime
    supported_actions: tuple[Literal["stop"], ...] = ()

    @model_validator(mode="after")
    def response_is_consistent(self) -> ArtifactJobResponse:
        if (self.operation_id is None) != (self.submit_request_id is None) or (
            self.state is None
        ) != (self.preparation is not None):
            raise InvalidValue(
                "artifact submission operation and request identity must be paired, "
                "and a job is either being prepared or has a lifecycle state",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        if self.state == "succeeded":
            if self.output_manifest_sha256 is None or self.result_evidence is None:
                raise InvalidValue(
                    "successful artifact job requires output manifest and result evidence",
                    reason=InvalidRequestReason.INCOMPLETE,
                )
            if self.status_reason is not None:
                raise InvalidValue(
                    "successful artifact job cannot retain a failure reason",
                    reason=InvalidRequestReason.CONFLICT,
                )
            try:
                _validate_outputs_against_contract(
                    self.compiled_contract,
                    tuple(
                        RecipeJobFile.parse(
                            item.model_dump(mode="json"), maximum_bytes=1024**3
                        )
                        for item in self.output_files
                    ),
                    terminal=True,
                )
            except ArtifactJobError as error:
                raise InvalidValue(
                    str(error), reason=InvalidRequestReason.MALFORMED
                ) from error
        if (
            self.state in {LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR}
            and not (self.status_reason or "").strip()
        ):
            raise InvalidValue(
                "failed or waiting artifact job requires a status reason",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        if (
            self.state == LifecycleState.NEEDS_OPERATOR
            and "stop" not in self.supported_actions
        ):
            # Rule 3 of the lifecycle core, as a contract: a job never waits for
            # an operator without an action the operator can take.
            raise InvalidValue(
                "a waiting artifact job must advertise its stop action",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        return self


class ArtifactJobListResponse(ArtifactJobContractModel):
    jobs: list[ArtifactJobResponse] = Field(max_length=100)


class ArtifactJobTransportCapabilities(ArtifactJobContractModel):
    max_input_files: int = Field(ge=1, le=32)
    max_input_file_bytes: int = Field(ge=1, le=MAX_INPUT_FILE_BYTES)
    max_input_total_bytes: int = Field(ge=1, le=1024**3)
    max_output_files: int = Field(ge=1, le=32)
    max_output_file_bytes: int = Field(ge=1, le=1024**3)
    max_output_total_bytes: int = Field(ge=1, le=2 * 1024**3)
    max_timeout_seconds: int = Field(ge=1, le=3_600)
    reserved_input_names: list[str] = Field(min_length=1, max_length=32)


class ArtifactJobStorageCapabilities(ArtifactJobContractModel):
    max_stored_bytes: int = Field(ge=1)
    used_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    in_flight_uploads: int = Field(ge=0)
    remaining_bytes: int = Field(ge=0)


class ArtifactJobCapabilitiesResponse(ArtifactJobContractModel):
    transport: ArtifactJobTransportCapabilities
    storage: ArtifactJobStorageCapabilities


@dataclass(frozen=True, slots=True)
class _JobLaunch:
    """What a job's submission needs from its stored evidence."""

    installed_document: dict[str, object]
    memory_floor_bytes: int
    contract: CompiledArtifactContract
    parameters: dict[str, object]
    input_files: list[object]


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
    compiled_contract: CompiledArtifactContract
    input_manifest_sha256: str
    input_total_bytes: int
    input_declarations: tuple[dict[str, object], ...]
    input_files: tuple[dict[str, object], ...]
    output_limits: dict[str, object]
    output_manifest_sha256: str | None
    output_files: tuple[dict[str, object], ...]
    result_evidence: dict[str, object] | None
    status_reason: str | None
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime
    #: The operator actions that apply now (``stop`` while the job is in doubt).
    supported_actions: tuple[str, ...] = ()


def _json_copy(value: object) -> object:
    return json.loads(canonical_message(value))


def _record_unservable_run(run_id: str, note: str) -> None:
    """Record why a run cannot take a job; the run itself is reconciled by the
    Run/Switch lifecycle, and the submit is refused request-led."""

    retire_as_unknown(
        "artifact-job.run",
        run_id,
        BookkeepingReason.PERSISTED_STATE_DAMAGED,
        note,
    )


def _recipe_interface(document: Mapping[str, object]) -> str | None:
    """The one artifact interface a recipe declares; ``None`` when it declares
    none or several (the recipe then does not serve the interface asked for)."""

    interfaces = document.get("interfaces")
    artifact_interfaces = (
        [
            item.get("adapter")
            for item in interfaces
            if isinstance(item, Mapping) and item.get("adapter") != "openai"
        ]
        if isinstance(interfaces, list)
        else []
    )
    if len(artifact_interfaces) != 1 or not isinstance(artifact_interfaces[0], str):
        return None
    return artifact_interfaces[0]


def _finite_parameter_number(value: object) -> bool:
    return (isinstance(value, float) and math.isfinite(value)) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _compile_contract(
    document: Mapping[str, object], interface_name: str
) -> CompiledArtifactContract | Damaged:
    """The contract a recipe document declares, or ``Damaged`` (with the reason)
    when it does not compile: the caller decides, a request refuses the recipe
    and a rebuild finds no evidence."""

    try:
        return compile_artifact_contract(document, interface_name)
    except (TypeError, ValueError) as error:
        return Damaged(str(error))


def _output_mappings(
    parsed: CompiledArtifactContract,
) -> list[dict[str, object]]:
    return [
        {
            "slot": slot.id,
            "media_type": slot.media_types[0],
            "extensions": list(slot.extensions),
        }
        for slot in parsed.output.slots
    ]


def _effective_output_limits(
    parsed: CompiledArtifactContract,
    supplied: Mapping[str, object],
) -> RecipeJobOutputLimits:
    try:
        requested = RecipeJobOutputLimits.parse(supplied)
        allowed = RecipeJobOutputLimits.parse(
            parsed.output_limits.model_dump(mode="json")
        )
    except (AgentProtocolError, KeyError, TypeError) as error:
        raise ArtifactJobInvalid(
            "artifact output limits are invalid", reason=InvalidRequestReason.MALFORMED
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


def _validate_inputs_against_contract(
    parsed: CompiledArtifactContract,
    inputs: tuple[RecipeJobInputFile, ...],
) -> None:
    slots = {item.id: item for item in parsed.input.slots}
    if not slots and inputs:
        raise ArtifactJobInvalid(
            "recipe does not accept artifact input files",
            reason=InvalidRequestReason.UNSUPPORTED,
        )
    for item in inputs:
        slot = slots.get(item.slot)
        if slot is None:
            raise ArtifactJobInvalid(
                f"artifact input slot {item.slot} is undeclared",
                reason=InvalidRequestReason.UNKNOWN_FIELD,
            )
        if item.media_type not in slot.media_types:
            raise ArtifactJobInvalid(
                f"artifact input {item.name} media type is not allowed",
                reason=InvalidRequestReason.UNSUPPORTED,
            )
        if item.size_bytes > slot.max_file_bytes:
            raise ArtifactJobInvalid(
                f"artifact input {item.name} exceeds its slot limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        extensions = slot.extensions
        if extensions and not any(
            item.name.lower().endswith(ext) for ext in extensions
        ):
            raise ArtifactJobInvalid(
                f"artifact input {item.name} extension is not allowed",
                reason=InvalidRequestReason.UNSUPPORTED,
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
            raise ArtifactJobInvalid(
                f"artifact output {output.name} has no unique slot",
                reason=InvalidRequestReason.MALFORMED,
            )
        longest = max(length for length, _slot in matches)
        longest_slots = {slot.id: slot for length, slot in matches if length == longest}
        if len(longest_slots) != 1:
            raise ArtifactJobInvalid(
                f"artifact output {output.name} has no unique slot",
                reason=InvalidRequestReason.MALFORMED,
            )
        slot = next(iter(longest_slots.values()))
        if output.size_bytes > slot.max_file_bytes:
            raise ArtifactJobInvalid(
                f"artifact output {output.name} exceeds its slot limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        assignments[slot.id].append(output)
    for slot in slots:
        selected = assignments[slot.id]
        minimum = slot.min_files if terminal else 0
        if not minimum <= len(selected) <= slot.max_files:
            raise ArtifactJobInvalid(
                f"artifact output slot {slot.id} file count is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if sum(item.size_bytes for item in selected) > slot.max_total_bytes:
            raise ArtifactJobInvalid(
                f"artifact output slot {slot.id} bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
    if sum(item.size_bytes for item in outputs) > parsed.output.max_total_bytes:
        raise ArtifactJobInvalid(
            "artifact output bytes exceed the recipe contract",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
        )


def _effective_parameters(
    contract: CompiledArtifactContract | Mapping[str, object],
    supplied: Mapping[str, object],
) -> dict[str, object]:
    if isinstance(contract, CompiledArtifactContract):
        by_name = {item.name: item for item in contract.parameters}
        if set(supplied) - set(by_name):
            raise ArtifactJobInvalid(
                "artifact job contains undeclared parameters",
                reason=InvalidRequestReason.UNKNOWN_FIELD,
            )
        effective: dict[str, object] = {}
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
                and value in definition.allowed_values
            )
            if not valid_type:
                raise ArtifactJobInvalid(
                    f"artifact job parameter {name} has the wrong type",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (
                    definition.minimum is not None
                    and value < definition.minimum
                    or definition.maximum is not None
                    and value > definition.maximum
                )
            ):
                raise ArtifactJobInvalid(
                    f"artifact job parameter {name} is outside its range",
                    reason=InvalidRequestReason.OUT_OF_RANGE,
                )
            if isinstance(definition.pattern, str) and isinstance(value, str):
                try:
                    matched = re.fullmatch(definition.pattern, value) is not None
                except re.error as error:
                    raise ArtifactJobInvalid(
                        "artifact parameter pattern is invalid",
                        reason=InvalidRequestReason.MALFORMED,
                    ) from error
                if not matched:
                    raise ArtifactJobInvalid(
                        f"artifact job parameter {name} does not match",
                        reason=InvalidRequestReason.MALFORMED,
                    )
            effective[name] = value
        return effective

    # Retain the narrow mapping form for the focused helper's isolated tests;
    # service paths always take the typed branch above.
    definitions = contract.get("parameters") if isinstance(contract, Mapping) else None
    if not isinstance(definitions, list):
        raise ArtifactJobInvalid(
            "artifact parameter contract is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    by_name = {
        item["name"]: item
        for item in definitions
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if set(supplied) - set(by_name):
        raise ArtifactJobInvalid(
            "artifact job contains undeclared parameters",
            reason=InvalidRequestReason.UNKNOWN_FIELD,
        )
    effective: dict[str, object] = {}
    for name, definition in by_name.items():
        value = supplied.get(name, definition.get("default"))
        kind = definition.get("type")
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
            and value in definition.get("allowed_values", [])
        )
        if not valid_type:
            raise ArtifactJobInvalid(
                f"artifact job parameter {name} has the wrong type",
                reason=InvalidRequestReason.MALFORMED,
            )
        minimum = definition.get("minimum")
        maximum = definition.get("maximum")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (
                isinstance(minimum, (int, float))
                and value < minimum
                or isinstance(maximum, (int, float))
                and value > maximum
            )
        ):
            raise ArtifactJobInvalid(
                f"artifact job parameter {name} is outside its range",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        pattern = definition.get("pattern")
        if isinstance(pattern, str) and isinstance(value, str):
            try:
                matched = re.fullmatch(pattern, value) is not None
            except re.error as error:
                raise ArtifactJobInvalid(
                    "artifact parameter pattern is invalid",
                    reason=InvalidRequestReason.MALFORMED,
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
) -> dict[str, object]:
    """Load persisted parameters as canonical JSON and enforce the contract.

    Parameter names and their scalar shapes come from the compiled recipe
    contract.  The persisted JSON object is therefore validated by the same
    declared-parameter path as a create request; this intentionally does not
    maintain a separate engine-key allowlist.
    """
    try:
        decoded = json.loads(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise ArtifactJobInvalid(
            "artifact job parameters must be a JSON object",
            reason=InvalidRequestReason.MALFORMED,
        ) from error
    if not isinstance(decoded, dict):
        raise ArtifactJobInvalid(
            "artifact job parameters must be a JSON object",
            reason=InvalidRequestReason.MALFORMED,
        )
    effective = _effective_parameters(contract, decoded)
    try:
        canonical = json.loads(canonical_message(effective))
    except (TypeError, ValueError) as error:
        raise ArtifactJobInvalid(
            "artifact job parameters are not canonical JSON",
            reason=InvalidRequestReason.MALFORMED,
        ) from error
    if not isinstance(canonical, dict):
        raise ArtifactJobInvalid(
            "artifact job parameters must be a JSON object",
            reason=InvalidRequestReason.MALFORMED,
        )
    return canonical


def _artifact_submission_in_session(
    session: Session, artifact_job: ArtifactJob
) -> Job | Residue | None:
    """The submission that owns a submitted job, ``None`` before it is submitted.

    A submission whose row, digest or request identity does not hold is damaged
    bookkeeping: it is retired as unknown and the job is shown without it.
    """

    if artifact_job.operation_id is None:
        return None
    operation_id = artifact_job.operation_id

    def read() -> Job | Damaged:
        submission = session.get(Job, operation_id)
        if submission is None or submission.kind != "recipe.job.run.v1":
            return Damaged("artifact job submission owner is invalid")
        payload = submission.payload
        if not isinstance(payload, Mapping):
            return Damaged("artifact job submission owner is invalid")
        payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()
        if (
            payload_digest != submission.payload_digest
            or payload.get("owner_kind") != "artifact-job"
            or payload.get("owner_id") != artifact_job.id
        ):
            return Damaged("artifact job submission owner is invalid")
        _UUID_ID_ADAPTER.validate_python(submission.request_id, strict=True)
        return submission

    return read_or_rebuild(
        kind="artifact-job.submission", subject=artifact_job.id, read=read
    )


class ArtifactJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        recipe_operations: RecipeOperationService,
        blob_store: ArtifactBlobStore,
        clock: Callable[[], datetime],
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        if not 3600 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise InvalidValue(
                "artifact job retention is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        self._sessions = sessions
        self._recipe_operations = recipe_operations
        self._blob_store = blob_store
        self._clock = clock
        self._retention_seconds = retention_seconds

    def reconcile_storage(self, *, batch_limit: int = 1000) -> dict[str, object]:
        if not 1 <= batch_limit <= 10_000:
            raise InvalidValue(
                "artifact reconciliation batch limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._blob_store.reference_reconciliation():
            return self._reconcile_storage_fenced(batch_limit=batch_limit)

    def _reconcile_storage_fenced(self, *, batch_limit: int) -> dict[str, object]:
        cutoff = self._clock() - timedelta(seconds=self._retention_seconds)
        with self._sessions.begin() as session:
            expired = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(
                        ArtifactJob.state.in_(ajs.ENDED),
                        ArtifactJob.completed_at.is_not(None),
                        ArtifactJob.completed_at < cutoff,
                    )
                    .limit(batch_limit)
                )
            )
            # Bytes may only be reclaimed because the exact job that referenced
            # them expired. An empty global reference scan proves nothing after a
            # restore that lost reference rows, so it never authorizes deletion.
            expired_blobs = (
                set(
                    session.scalars(
                        select(ArtifactJobFile.blob_sha256).where(
                            ArtifactJobFile.artifact_job_id.in_(
                                [job.id for job in expired]
                            )
                        )
                    )
                )
                if expired
                else set()
            )
            if expired:
                session.execute(
                    delete(ArtifactJobFile).where(
                        ArtifactJobFile.artifact_job_id.in_([job.id for job in expired])
                    )
                )
            for job in expired:
                session.delete(job)
            session.flush()
            referenced = set(session.scalars(select(ArtifactJobFile.blob_sha256)))
            reclaimable = expired_blobs - referenced
            orphan_rows = (
                tuple(
                    session.scalars(
                        select(ArtifactJobBlob)
                        .where(ArtifactJobBlob.sha256.in_(reclaimable))
                        .limit(batch_limit)
                    )
                )
                if reclaimable
                else ()
            )
            for blob in orphan_rows:
                session.delete(blob)
        # The store may unlink only the digests this pass proved reclaimable.
        # Passing the full reference set at all is what let an incomplete
        # reference scan delete live bytes, so the evidence is now explicit.
        result = self._blob_store.reconcile(
            referenced,
            batch_limit=batch_limit,
            reclaimable_sha256=reclaimable,
            _reference_fenced=True,
        )
        return {
            "expired_jobs": len(expired),
            "removed_blob_records": len(orphan_rows),
            **result,
            "remaining_work": bool(
                len(expired) == batch_limit
                or len(orphan_rows) == batch_limit
                or result.get("remaining_work") is True
            ),
        }

    def create(
        self,
        run_id: str,
        *,
        interface: str,
        parameters: Mapping[str, object],
        inputs: Sequence[Mapping[str, object]],
        output_limits: Mapping[str, object],
        timeout_seconds: int,
        actor: str,
        request_id: str,
    ) -> ArtifactJobView:
        parsed_inputs = tuple(
            sorted(
                (
                    RecipeJobInputFile.parse(item, maximum_bytes=MAX_INPUT_FILE_BYTES)
                    for item in inputs
                ),
                key=lambda item: item.name.encode("utf-8"),
            )
        )
        if len(parsed_inputs) > MAX_INPUT_FILES:
            raise ArtifactJobInvalid(
                "artifact job has too many input files",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        if len({item.name for item in parsed_inputs}) != len(parsed_inputs):
            raise ArtifactJobInvalid(
                "artifact input names must be unique",
                reason=InvalidRequestReason.DUPLICATE,
            )
        total = sum(item.size_bytes for item in parsed_inputs)
        if total > MAX_INPUT_TOTAL_BYTES:
            raise ArtifactJobInvalid(
                "artifact job input bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ArtifactJobInvalid(
                "artifact job timeout is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if not 1 <= timeout_seconds <= 3_600:
            raise ArtifactJobInvalid(
                "artifact job timeout is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        supplied_parameters = _json_copy(parameters)
        if not isinstance(supplied_parameters, dict):
            raise ArtifactJobInvalid(
                "artifact job parameters must be an object",
                reason=InvalidRequestReason.MALFORMED,
            )
        manifest = RecipeJobInputManifest(
            schema_version=1, total_bytes=total, files=list(parsed_inputs)
        ).model_dump(mode="json")
        manifest_digest = recipe_job_manifest_sha256(parsed_inputs)
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                )
        except IntegrityError:
            # A concurrent controller process may win the globally unique
            # request key after our initial lookup. Re-open a transaction and
            # apply the same semantic replay comparison to the committed row.
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(ArtifactJob).where(ArtifactJob.request_id == request_id)
                )
                if existing is None:
                    raise ArtifactJobInvalid(
                        "artifact job request key collision",
                        reason=InvalidRequestReason.CONFLICT,
                    ) from None
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                    existing=existing,
                )

    def _create_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        interface: str,
        supplied_parameters: Mapping[str, object],
        parsed_inputs: tuple[RecipeJobInputFile, ...],
        output_limits: Mapping[str, object],
        timeout_seconds: int,
        actor: str,
        request_id: str,
        manifest: dict[str, object],
        manifest_digest: str,
        total: int,
        now: datetime,
        existing: ArtifactJob | None = None,
    ) -> ArtifactJobView:
        existing = existing or session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.request_id == request_id)
            .with_for_update()
        )
        if existing is not None and (
            existing.run_id != run_id
            or existing.interface != interface
            or existing.input_manifest != manifest
            or existing.input_manifest_sha256 != manifest_digest
            or existing.input_total_bytes != total
            or existing.timeout_seconds != timeout_seconds
            or existing.actor != actor
        ):
            raise ArtifactJobInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        run = session.get(RecipeRun, run_id)
        if run is None or existing is None and run.state != RunState.RUNNING:
            raise ArtifactJobInvalid(
                "recipe run is not accepting jobs",
                reason=InvalidRequestReason.NOT_READY,
            )
        installation = session.get(RecipeInstallation, run.installation_id)
        resolved = (
            _active_recipe_revision(session, installation.recipe_revision_id)
            if installation is not None
            else None
        )
        if resolved is None:
            raise ArtifactJobInvalid(
                "recipe revision is unavailable", reason=InvalidRequestReason.NOT_FOUND
            )
        _revision, recipe = resolved
        document = recipe.model_dump(mode="json")
        if _recipe_interface(document) != interface or interface == "openai":
            if existing is not None:
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            raise ArtifactJobInvalid(
                "artifact job interface does not match the run",
                reason=InvalidRequestReason.CONFLICT,
            )
        try:
            contract = _compile_contract(document, interface)
            if isinstance(contract, Damaged):
                raise ArtifactJobInvalid(
                    contract.note, reason=InvalidRequestReason.UNSUPPORTED
                )
            contract_digest = contract.sha256()
            parameters_copy = _effective_parameters(contract, supplied_parameters)
            limits = _effective_output_limits(contract, output_limits)
            if timeout_seconds > contract.max_timeout_seconds:
                raise ArtifactJobInvalid(
                    "artifact job timeout exceeds the recipe contract",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            _validate_inputs_against_contract(contract, parsed_inputs)
        except ArtifactJobError:
            if existing is not None:
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                ) from None
            raise
        effective_limits = limits.to_mapping()
        contract_mapping = contract.to_mapping()
        if existing is not None:
            if (
                existing.parameters != parameters_copy
                or existing.output_limits != effective_limits
                or existing.compiled_contract != contract_mapping
                or existing.contract_sha256 != contract_digest
            ):
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return self._view_in_session(session, existing)
        artifact_job = ArtifactJobAdapter.new_job(
            id=str(uuid.uuid4()),
            run_id=run_id,
            request_id=request_id,
            interface=interface,
            parameters=parameters_copy,
            output_limits=effective_limits,
            compiled_contract=contract_mapping,
            contract_sha256=contract_digest,
            input_manifest=manifest,
            input_manifest_sha256=manifest_digest,
            input_total_bytes=total,
            timeout_seconds=timeout_seconds,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(artifact_job)
        session.flush()
        return self._view_in_session(session, artifact_job)

    def capabilities(self) -> dict[str, object]:
        return {
            "transport": {
                "max_input_files": MAX_INPUT_FILES,
                "max_input_file_bytes": MAX_INPUT_FILE_BYTES,
                "max_input_total_bytes": MAX_INPUT_TOTAL_BYTES,
                "max_output_files": 32,
                "max_output_file_bytes": 1024**3,
                "max_output_total_bytes": 2 * 1024**3,
                "max_timeout_seconds": 3_600,
                "reserved_input_names": ["manifest.json"],
            },
            "storage": self._blob_store.usage(),
        }

    def put_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> ArtifactJobView:
        with self._blob_store.reference_attachment():
            try:
                stored = self._blob_store.put_bytes(
                    expected_sha256, content, maximum_bytes=MAX_INPUT_FILE_BYTES
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            return self._attach_input(
                job_id, name=name, media_type=media_type, stored=stored
            )

    async def put_input_stream(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> ArtifactJobView:
        expected_bytes = self.input_upload_size(
            job_id, name=name, media_type=media_type, expected_sha256=expected_sha256
        )
        if content_length != expected_bytes:
            raise ArtifactJobInvalid(
                "artifact input Content-Length does not match",
                reason=InvalidRequestReason.CONFLICT,
            )
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=expected_bytes,
                    maximum_bytes=MAX_INPUT_FILE_BYTES,
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            return self._attach_input(
                job_id, name=name, media_type=media_type, stored=stored
            )

    def input_upload_size(
        self, job_id: str, *, name: str, media_type: str, expected_sha256: str
    ) -> int:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job inputs are immutable",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            declaration = self._input_declaration(session, job, name)
            size_bytes = None if declaration is None else declaration.get("size_bytes")
            if (
                declaration is None
                or declaration.get("media_type") != media_type
                or declaration.get("sha256") != expected_sha256
                or not isinstance(size_bytes, int)
            ):
                raise ArtifactJobInvalid(
                    "artifact input does not match its declaration",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return size_bytes

    def _attach_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        stored: StoredArtifactBlob,
    ) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job inputs are immutable",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            declaration = self._input_declaration(session, job, name)
            if (
                declaration is None
                or declaration.get("media_type") != media_type
                or declaration.get("sha256") != stored.sha256
                or declaration.get("size_bytes") != stored.size_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact input does not match its declaration",
                    reason=InvalidRequestReason.CONFLICT,
                )
            self._put_blob_in_session(session, stored, now)
            existing = self._file_in_session(session, job_id, "input", name)
            if existing is not None:
                if existing.blob_sha256 != stored.sha256:
                    raise ArtifactJobInvalid(
                        "artifact input changed", reason=InvalidRequestReason.CONFLICT
                    )
                return self._view_in_session(session, job)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="input",
                    slot=str(declaration["slot"]),
                    name=name,
                    media_type=media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now
            session.flush()
            return self._view_in_session(session, job)

    def finalize(self, job_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) == ajs.READY:
                return self._view_in_session(session, job)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job cannot be finalized",
                    reason=InvalidRequestReason.NOT_READY,
                )
            manifest = self._stored_input_manifest(session, job)
            uploaded = self._files_in_session(session, job_id, "input")
            observed = self._input_mappings(uploaded, manifest)
            if (
                isinstance(manifest, Residue)
                or manifest.model_dump(mode="json")["files"] != observed
            ):
                raise ArtifactJobInvalid(
                    "artifact job inputs are incomplete",
                    reason=InvalidRequestReason.INCOMPLETE,
                )
            ArtifactJobAdapter.mark_ready(job, now)
            return self._view_in_session(session, job)

    def submit(self, job_id: str, *, actor: str, request_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            artifact_job = session.get(ArtifactJob, job_id, with_for_update=True)
            if artifact_job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if artifact_job.operation_id is not None:
                submission = _artifact_submission_in_session(session, artifact_job)
                # A submission whose identity cannot be read is recorded; the job
                # is already submitted, so its view is the answer.
                if isinstance(submission, Job) and submission.request_id != request_id:
                    raise ArtifactJobInvalid(
                        "artifact job was submitted under another request identity",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._view_in_session(session, artifact_job)
            if ajs.preparation_of(artifact_job) != ajs.READY:
                raise ArtifactJobInvalid(
                    "artifact job is not ready", reason=InvalidRequestReason.NOT_READY
                )
            run = session.get(RecipeRun, artifact_job.run_id, with_for_update=True)
            if run is None or run.state != RunState.RUNNING:
                raise ArtifactJobInvalid(
                    "recipe run is not accepting jobs",
                    reason=InvalidRequestReason.NOT_READY,
                )
            concurrent = session.scalar(
                select(ArtifactJob.id)
                .where(
                    ArtifactJob.run_id == run.id,
                    ArtifactJob.id != artifact_job.id,
                    ArtifactJob.state.in_(ajs.LIVE),
                )
                .limit(1)
            )
            if concurrent is not None:
                raise ArtifactJobInvalid(
                    "another artifact job already owns this run reservation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            installation = session.get(RecipeInstallation, run.installation_id)
            resolved = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if installation is None or resolved is None:
                # The run's installation or recipe revision no longer exists:
                # a genuine absence, refused request-led with its own code.
                raise ArtifactJobInvalid(
                    "recipe job workload identity is unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            revision, _recipe = resolved
            node = self._job_node_in_session(session, run)
            launch = self._job_launch(session, artifact_job, run, installation, node)
            if launch is None:
                # The run's or the job's stored evidence is damaged and nothing
                # re-derives it (each case is recorded as residue): the run is
                # not accepting this job now, and the submit is asked again.
                raise ArtifactJobInvalid(
                    "recipe run is not accepting jobs",
                    reason=InvalidRequestReason.NOT_READY,
                )
            contract = launch.contract
            floor = launch.memory_floor_bytes
            parameters = launch.parameters
            installed_document = launch.installed_document
            mapping = (
                session.get(ClusterMapping, run.mapping_id)
                if run.mapping_id is not None
                else None
            )
            invocation = compile_job_invocation(
                session,
                revision=revision,
                installed=installed_document,
                build=(
                    session.get(RecipeBuild, installation.recipe_build_id)
                    if installation.recipe_build_id is not None
                    else None
                ),
                parameters=parameters,
                timeout_seconds=artifact_job.timeout_seconds,
                memory_floor_bytes=floor,
                reserved_memory_bytes=node.reserved_memory_bytes,
                option_choices=mapping_option_choices(
                    mapping.parameters if mapping is not None else {}
                ),
            )
            raw_files = launch.input_files
            payload = {
                "job_id": artifact_job.id,
                "run_id": run.id,
                "installation_id": installation.id,
                "recipe_revision_id": revision.id,
                "plan_digest": run.plan_digest,
                "mapping_id": run.mapping_id,
                "input_manifest_sha256": artifact_job.input_manifest_sha256,
                "input_total_bytes": artifact_job.input_total_bytes,
                "inputs": raw_files,
                "compiled_execution_plan": invocation,
                "run_generation": run.run_generation,
                "output_mappings": _output_mappings(contract),
                "output_limits": artifact_job.output_limits,
            }
            RecipeJobRunRequest.parse(payload)
            operation = self._recipe_operations.enqueue_one_shot_job_in_session(
                session,
                artifact_job_id=artifact_job.id,
                run_id=run.id,
                node_id=node.node_id,
                payload=payload,
                actor=actor,
                request_id=request_id,
                authority_digest=revision.content_digest,
                now=now,
            )
            ArtifactJobAdapter.mark_submitted(artifact_job, operation.id, now)
        self._recipe_operations.notify_agents()
        return self.get(job_id)

    def get(self, job_id: str) -> ArtifactJobView:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view_in_session(session, job)

    def get_by_request_id(self, request_id: str) -> ArtifactJobView:
        """Resolve the original draft after a create response was lost."""
        with self._sessions() as session:
            job = session.scalar(
                select(ArtifactJob).where(ArtifactJob.request_id == request_id)
            )
            if job is None:
                raise MissingRecord(request_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view_in_session(session, job)

    def list_for_run(
        self, run_id: str, *, limit: int = 100
    ) -> tuple[ArtifactJobView, ...]:
        if not 1 <= limit <= 100:
            raise ArtifactJobInvalid(
                "artifact job list limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._sessions() as session:
            if session.get(RecipeRun, run_id) is None:
                raise MissingRecord(run_id, reason=InvalidRequestReason.NOT_FOUND)
            jobs = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(ArtifactJob.run_id == run_id)
                    .order_by(ArtifactJob.created_at.desc(), ArtifactJob.id.desc())
                    .limit(limit)
                )
            )
            return tuple(self._view_in_session(session, job) for job in jobs)

    def cancel(
        self, job_id: str, *, actor: str, request_id: str, reason: str
    ) -> ArtifactJobView:
        cancellation_reason = " ".join(reason.split())[:512]
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            operation_id = job.operation_id
            state = ajs.state_of(job)
            evidence = _result_evidence(job.result_evidence)
        if state in {ajs.SUCCEEDED, ajs.FAILED}:
            raise ArtifactJobInvalid(
                "artifact job is not cancellable", reason=InvalidRequestReason.CONFLICT
            )
        if state == ajs.CANCELLED and operation_id is None:
            if (
                evidence is not None
                and evidence.get("cancel_request_id") == request_id
                and evidence.get("cancel_actor") == actor
                and evidence.get("cancel_reason") == cancellation_reason
            ):
                return self.get(job_id)
            raise ArtifactJobInvalid(
                "cancellation request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        if operation_id is not None:
            try:
                self._recipe_operations.cancel(
                    operation_id, actor=actor, request_id=request_id, reason=reason
                )
            except RecipeOperationConflict as error:
                raise ArtifactJobInvalid(
                    str(error), reason=InvalidRequestReason.CONFLICT
                ) from error
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            assert job is not None
            if not ajs.is_ended(job):
                adapter = ArtifactJobAdapter(session, clock=self._clock)
                evidence = {
                    "cancel_request_id": request_id,
                    "cancel_actor": actor,
                    "cancel_reason": cancellation_reason,
                }
                if job.operation_id is None:
                    # Nothing was ever issued: the core cancels it at once.
                    adapter.settle(
                        job,
                        CancelRequested(request_id, cancellation_reason),
                        now,
                        reason=cancellation_reason,
                        evidence=evidence,
                    )
                else:
                    # The order carries the cancel; the job mirrors what it decided
                    # (``cancelling`` until the agent's receipt, or until the core
                    # ends the order after its stop budget).
                    adapter.project(
                        job, now, reason=cancellation_reason, evidence=evidence
                    )
            return self._view_in_session(session, job)

    def input_blob(
        self, job_id: str, sha256: str, *, node_id: str
    ) -> tuple[Path, str, int]:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job.id,
                    ArtifactJobFile.direction == "input",
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            if row is None:
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            blob = session.get(ArtifactJobBlob, sha256)
            if blob is None:
                # The file row has no blob row: the bytes are unknown, not a
                # refusal. The reader is told "not found" and re-uploads.
                retire_as_unknown(
                    "artifact-job.blob",
                    sha256,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "stored input has no blob row",
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            if path is None:
                # Bytes the store no longer holds are unknown, not a refusal: the
                # reader is told "not found" and the content is re-uploaded.
                retire_as_unknown(
                    "artifact-job.blob", sha256, note="stored bytes are absent"
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            return path, row.media_type, row.size_bytes

    def put_output(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": len(content),
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        with self._blob_store.reference_attachment():
            try:
                stored = self._blob_store.put_bytes(
                    expected_sha256, content, maximum_bytes=1024**3
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            self._attach_output(job_id, node_id=node_id, parsed=parsed, stored=stored)

    async def put_output_stream(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": content_length,
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        self._validate_output_upload(job_id, node_id=node_id, parsed=parsed)
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=content_length,
                    maximum_bytes=1024**3,
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            self._attach_output(job_id, node_id=node_id, parsed=parsed, stored=stored)

    def _validate_output_upload(
        self, job_id: str, *, node_id: str, parsed: RecipeJobFile
    ) -> None:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            existing = self._files_in_session(session, job_id, "output")
            projected = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in existing
                if item.name != parsed.name
            ) + (parsed,)
            contract = self._stored_contract(session, job)
            if isinstance(contract, Residue):
                # The job's contract is unreadable and nothing re-derives it:
                # its transfers close and the job ends through its own lifecycle.
                raise ArtifactJobTransferClosedError("artifact job transfer is closed")
            _validate_outputs_against_contract(contract, projected, terminal=False)
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobInvalid(
                    "artifact output media type is not allowed",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            if (
                len(existing)
                + (0 if any(item.name == parsed.name for item in existing) else 1)
                > limits.max_files
            ):
                raise ArtifactJobInvalid(
                    "artifact output file count exceeds the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing if item.name != parsed.name)
                + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact output bytes exceed the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )

    def _attach_output(
        self,
        job_id: str,
        *,
        node_id: str,
        parsed: RecipeJobFile,
        stored: StoredArtifactBlob,
    ) -> None:
        if stored.sha256 != parsed.sha256 or stored.size_bytes != parsed.size_bytes:
            raise ArtifactJobInvalid(
                "stored artifact output does not match declaration",
                reason=InvalidRequestReason.CONFLICT,
            )
        now = self._clock()
        with self._sessions.begin() as session:
            job = self._authorized_agent_job(session, job_id, node_id, lock=True)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobInvalid(
                    "artifact output media type is not allowed",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            existing = self._files_in_session(session, job_id, "output")
            same_name = next(
                (item for item in existing if item.name == parsed.name), None
            )
            if same_name is not None:
                if same_name.blob_sha256 != parsed.sha256:
                    raise ArtifactJobInvalid(
                        "artifact output changed", reason=InvalidRequestReason.CONFLICT
                    )
                return
            projected = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in existing
            ) + (parsed,)
            contract = self._stored_contract(session, job)
            if isinstance(contract, Residue):
                # The job's contract is unreadable and nothing re-derives it:
                # its transfers close and the job ends through its own lifecycle.
                raise ArtifactJobTransferClosedError("artifact job transfer is closed")
            _validate_outputs_against_contract(contract, projected, terminal=False)
            if len(existing) + 1 > limits.max_files:
                raise ArtifactJobInvalid(
                    "artifact output file count exceeds the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing) + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact output bytes exceed the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            self._put_blob_in_session(session, stored, now)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="output",
                    slot=None,
                    name=parsed.name,
                    media_type=parsed.media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now

    def result_blob(
        self, job_id: str, name: str, sha256: str
    ) -> tuple[Path, str, str, int]:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.state_of(job) != ajs.SUCCEEDED:
                raise ArtifactJobInvalid(
                    "artifact job result is not available",
                    reason=InvalidRequestReason.NOT_READY,
                )
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == "output",
                    ArtifactJobFile.name == name,
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            blob = session.get(ArtifactJobBlob, sha256) if row is not None else None
            if row is None or blob is None:
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            if path is None:
                # Bytes the store no longer holds are unknown, not a refusal: the
                # reader is told "not found" and the content is re-uploaded.
                retire_as_unknown(
                    "artifact-job.blob", sha256, note="stored bytes are absent"
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            return path, row.media_type, row.name, row.size_bytes

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        parent = session.get(Job, operation.parent_job_id)
        if parent is None or parent.kind != "recipe.job.run.v1":
            return
        artifact_job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == parent.id)
            .with_for_update(of=ArtifactJob)
        )
        if artifact_job is None:
            # A result for an order whose artifact job row is gone has nothing to
            # apply: recorded as unknown, and the order's own lifecycle ends it.
            retire_as_unknown(
                "artifact-job.result",
                operation.parent_job_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "no artifact job owns this order",
            )
            return
        state = getattr(message, "state", None)
        raw_result = getattr(message, "result", None)
        now = self._clock()
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        if state == agent_operation_states.WIRE_UNKNOWN:
            try:
                waiting_result = RecipeJobRunResult.parse(raw_result)
                if (
                    waiting_result.job_id != artifact_job.id
                    or waiting_result.run_id != artifact_job.run_id
                    or waiting_result.exit_code != 130
                    or waiting_result.outputs
                ):
                    raise ArtifactResultInvalid(
                        "waiting artifact result identity or output is invalid",
                        reason=InvalidRequestReason.MALFORMED,
                    )
            except (AgentProtocolError, TypeError, ValueError) as error:
                self._reject_result(
                    adapter, operation, parent, artifact_job, error, now
                )
                return
            # The agent could not confirm that the job stopped.  The core has
            # already decided the order (an uncertain report under a cancel is
            # stopped and observed, and ends ``cancelled`` with the effect unknown
            # after its stop budget); the job mirrors that decision and keeps the
            # report as evidence.  It never waits for an operator on its own.
            adapter.project(
                artifact_job,
                now,
                reason=(
                    waiting_result.reason
                    if waiting_result.reason
                    else "artifact cancellation could not safely stop the active scope"
                ),
                evidence={
                    "failure_kind": "cancellation-stop-uncertain",
                    "recoverable": True,
                    "active_scope_may_remain": True,
                    "elapsed_milliseconds": waiting_result.elapsed_milliseconds,
                    "peak_memory_bytes": waiting_result.peak_memory_bytes,
                },
            )
            return
        try:
            result = RecipeJobRunResult.parse(raw_result)
            if result.job_id != artifact_job.id or result.run_id != artifact_job.run_id:
                raise ArtifactResultRefused(
                    "artifact result identity does not match",
                    reason=SecurityRefusalReason.AGENT_IDENTITY_MISMATCH,
                )
            uploaded = self._files_in_session(session, artifact_job.id, "output")
            observed = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in uploaded
            )
            limits = RecipeJobOutputLimits.parse(artifact_job.output_limits)
            if tuple(item.to_mapping() for item in result.outputs) != tuple(
                item.to_mapping() for item in observed
            ):
                raise ArtifactResultInvalid(
                    "artifact result does not match uploaded outputs",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if any(
                item.media_type not in limits.allowed_media_types
                for item in result.outputs
            ):
                raise ArtifactResultInvalid(
                    "artifact result media type is not allowed",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if (
                len(result.outputs) > limits.max_files
                or sum(item.size_bytes for item in result.outputs)
                > limits.max_total_bytes
            ):
                raise ArtifactResultInvalid(
                    "artifact result exceeds output limits",
                    reason=InvalidRequestReason.MALFORMED,
                )
            succeeded = state == "succeeded" and result.exit_code == 0
            failed = state == "failed" and result.exit_code != 0
            cancelled = bool(
                state == "cancelled"
                and result.exit_code == 130
                and not result.outputs
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if cancelled and uploaded:
                raise ArtifactResultInvalid(
                    "cancelled artifact result cannot retain uploaded outputs",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if succeeded:
                result_contract = self._stored_contract(session, artifact_job)
                if isinstance(result_contract, Residue):
                    raise ArtifactResultInvalid(
                        "artifact job contract is unreadable",
                        reason=InvalidRequestReason.MALFORMED,
                    )
                _validate_outputs_against_contract(
                    result_contract, result.outputs, terminal=True
                )
            if not (succeeded or failed or cancelled):
                raise ArtifactResultInvalid(
                    "artifact result state and exit code disagree",
                    reason=InvalidRequestReason.MALFORMED,
                )
        except (AgentProtocolError, TypeError, ValueError) as error:
            self._reject_result(adapter, operation, parent, artifact_job, error, now)
            return
        adapter.settle(
            artifact_job,
            Reported(
                Outcome.DONE
                if succeeded
                else Outcome.CANCELLED
                if cancelled
                else Outcome.FAILED,
                retryable=False,
            ),
            now,
            reason=(
                None
                if succeeded
                else result.reason
                or ("artifact job cancelled" if cancelled else "recipe job failed")
            ),
            evidence={
                "elapsed_milliseconds": result.elapsed_milliseconds,
                "peak_memory_bytes": result.peak_memory_bytes,
            },
            output_manifest_sha256=result.output_manifest_sha256,
        )

    @staticmethod
    def _reject_result(
        adapter: ArtifactJobAdapter,
        operation: AgentOperation,
        parent: Job,
        artifact_job: ArtifactJob,
        error: Exception,
        now: datetime,
    ) -> None:
        """A result that breaks the contract ends the order and the job, failed."""

        AgentOperationAdapter(adapter.session).record_outcome(
            operation, None, parent, Outcome.FAILED, now
        )
        adapter.settle(
            artifact_job,
            Reported(Outcome.FAILED, retryable=False),
            now,
            reason=str(error)[:512],
        )

    def _authorized_agent_job(
        self, session: Session, job_id: str, node_id: str, *, lock: bool = False
    ) -> ArtifactJob:
        statement = select(ArtifactJob).where(ArtifactJob.id == job_id)
        if lock:
            statement = statement.with_for_update(of=ArtifactJob)
        job = session.scalar(statement)
        if job is None or job.operation_id is None:
            raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
        parent = session.get(Job, job.operation_id)
        if parent is None or node_id not in parent.targets:
            raise ArtifactJobRefused(
                "agent is not authorized for this artifact job",
                reason=SecurityRefusalReason.FORBIDDEN,
            )
        order = session.scalar(
            select(AgentOperation.state).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        if ajs.state_of(job) not in ajs.QUEUED_OR_RUNNING or order not in {
            "queued",
            "running",
        }:
            # A job that ended, or whose attempt lapsed (its order is only being
            # observed), is fenced: its bytes are no longer accepted.
            raise ArtifactJobTransferClosedError("artifact job transfer is closed")
        return job

    def _job_launch(
        self,
        session: Session,
        artifact_job: ArtifactJob,
        run: RecipeRun,
        installation: RecipeInstallation,
        node: RunNode,
    ) -> _JobLaunch | None:
        """The stored evidence a submission needs, or ``None`` when it is damaged.

        Rebuilt from evidence, not from the stored run plan: the accepted capacity
        promise is the run node's own reservation, the memory floor is carried by
        the installed compiled plan, the contract is recompiled from its recipe
        and the declared inputs from their uploads.  What nothing re-derives is
        recorded as residue and read as ``None``.
        """

        try:
            installation_plan = parse_stored_installation_plan(installation.plan)
        except RecipeExecutionContractError as error:
            _record_unservable_run(run.id, f"stored installation plan: {error}")
            return None
        compiled = installation_plan.compiled_execution_plans.get(node.node_id)
        if compiled is None:
            _record_unservable_run(run.id, "no compiled plan for the job node")
            return None
        installed_document = compiled.model_dump(mode="json")
        placement = installed_document.get("runtime", {}).get("placement", {})
        floor = placement.get("memory_floor_bytes")
        if type(floor) is not int:
            _record_unservable_run(run.id, "installed plan has no memory floor")
            return None
        contract = self._stored_contract(session, artifact_job)
        manifest = self._stored_input_manifest(session, artifact_job)
        if isinstance(contract, Residue) or isinstance(manifest, Residue):
            return None
        try:
            parameters = _canonical_declared_parameters(
                contract, artifact_job.parameters
            )
        except (ArtifactJobError, TypeError, ValueError) as error:
            _record_unservable_run(run.id, f"stored job parameters: {error}")
            return None
        return _JobLaunch(
            installed_document=installed_document,
            memory_floor_bytes=floor,
            contract=contract,
            parameters=parameters,
            input_files=list(manifest.model_dump(mode="json")["files"]),
        )

    @staticmethod
    def _job_node_in_session(session: Session, run: RecipeRun) -> RunNode:
        nodes = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
            )
        )

        def planned_endpoints() -> set[str]:
            return {
                node.node_id
                for node in parse_stored_run_plan(run.plan).nodes
                if node.endpoint_owner
            }

        def mapped_endpoints() -> set[str] | None:
            # Rebuild from evidence: the cluster mapping names each node's role
            # in the run, independent of the stored run plan.
            if run.mapping_id is None:
                return None
            return set(
                session.scalars(
                    select(ClusterMappingNode.node_id).where(
                        ClusterMappingNode.mapping_id == run.mapping_id,
                        ClusterMappingNode.endpoint_owner.is_(True),
                    )
                )
            )

        loaded = read_or_rebuild(
            kind="artifact-job.run-plan",
            subject=run.id,
            read=planned_endpoints,
            rebuild=mapped_endpoints,
        )
        endpoint_ids = set() if isinstance(loaded, Residue) else loaded
        candidates = [node for node in nodes if node.node_id in endpoint_ids]
        if not candidates and len(nodes) == 1:
            candidates = list(nodes)
        if len(candidates) != 1 or candidates[0].state != "running":
            # The run's endpoint owner is not running (yet, or any more): the
            # run is not accepting jobs, which is the requester's to retry.
            raise ArtifactJobInvalid(
                "artifact job endpoint owner is not running",
                reason=InvalidRequestReason.NOT_READY,
            )
        return candidates[0]

    @staticmethod
    def _put_blob_in_session(
        session: Session, stored: StoredArtifactBlob, now: datetime
    ) -> None:
        blob = session.get(ArtifactJobBlob, stored.sha256)
        if blob is not None:
            if (
                blob.size_bytes != stored.size_bytes
                or blob.storage_key != stored.storage_key
            ):
                raise ArtifactJobRefused(
                    "content-addressed artifact collision",
                    reason=SecurityRefusalReason.DIGEST_MISMATCH,
                )
            return
        session.add(
            ArtifactJobBlob(
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                storage_key=stored.storage_key,
                created_at=now,
            )
        )

    @staticmethod
    def _file_in_session(
        session: Session, job_id: str, direction: str, name: str
    ) -> ArtifactJobFile | None:
        return session.scalar(
            select(ArtifactJobFile).where(
                ArtifactJobFile.artifact_job_id == job_id,
                ArtifactJobFile.direction == direction,
                ArtifactJobFile.name == name,
            )
        )

    def _stored_input_manifest(
        self, session: Session, job: ArtifactJob
    ) -> RecipeJobInputManifest | Residue:
        """The job's declared inputs: stored, else rebuilt from its uploaded rows.

        The rebuilt manifest is evidence only when it has the digest the job was
        created under; otherwise the damaged manifest is retired as unknown.
        """

        def rebuild() -> RecipeJobInputManifest | None:
            rows = self._files_in_session(session, job.id, "input")
            if not rows or any(row.slot is None for row in rows):
                return None
            files = sorted(
                (
                    RecipeJobInputFile(
                        slot=row.slot or "",
                        name=row.name,
                        media_type=row.media_type,
                        size_bytes=row.size_bytes,
                        sha256=row.blob_sha256,
                    )
                    for row in rows
                ),
                key=lambda item: item.name.encode("utf-8"),
            )
            rebuilt = RecipeJobInputManifest(
                schema_version=1,
                total_bytes=sum(item.size_bytes for item in files),
                files=files,
            )
            return (
                rebuilt
                if rebuilt.total_bytes == job.input_total_bytes
                and recipe_job_manifest_sha256(tuple(files))
                == job.input_manifest_sha256
                else None
            )

        return read_or_rebuild(
            kind="artifact-job.input-manifest",
            subject=job.id,
            read=lambda: _read_input_manifest(job),
            rebuild=rebuild,
        )

    def _input_declaration(
        self, session: Session, job: ArtifactJob, name: str
    ) -> Mapping[str, object] | None:
        manifest = self._stored_input_manifest(session, job)
        if isinstance(manifest, Residue):
            return None
        return next(
            (
                item.model_dump(mode="json")
                for item in manifest.files
                if item.name == name
            ),
            None,
        )

    @staticmethod
    def _files_in_session(
        session: Session, job_id: str, direction: str
    ) -> tuple[ArtifactJobFile, ...]:
        return tuple(
            session.scalars(
                select(ArtifactJobFile)
                .where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == direction,
                )
                .order_by(ArtifactJobFile.name)
            )
        )

    @staticmethod
    def _file_mapping(item: ArtifactJobFile) -> dict[str, object]:
        return {
            "name": item.name,
            "media_type": item.media_type,
            "size_bytes": item.size_bytes,
            "sha256": item.blob_sha256,
        }

    @staticmethod
    def _input_mappings(
        rows: Sequence[ArtifactJobFile],
        manifest: RecipeJobInputManifest | Residue,
    ) -> list[dict[str, object]]:
        """The uploaded inputs with their slots.

        A row that lost its slot takes it from the declared manifest (the slot
        is declared by name); one the manifest cannot place is left out and
        recorded, so it reads as not uploaded and is uploaded again.
        """

        declared = (
            {}
            if isinstance(manifest, Residue)
            else {item.name: item.slot for item in manifest.files}
        )
        mappings: list[dict[str, object]] = []
        for row in rows:
            slot = row.slot if row.slot is not None else declared.get(row.name)
            if slot is None:
                retire_as_unknown(
                    "artifact-job.input-slot",
                    row.artifact_job_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    f"uploaded input {row.name} has no slot",
                )
                continue
            mappings.append({"slot": slot, **ArtifactJobService._file_mapping(row)})
        return mappings

    def _stored_contract(
        self, session: Session, job: ArtifactJob
    ) -> CompiledArtifactContract | Residue:
        """The job's compiled contract: stored, else recompiled from its recipe.

        The recompiled contract is evidence only when it has the digest the job
        was created under; otherwise the damaged contract is retired as unknown.
        """

        def rebuild() -> CompiledArtifactContract | None:
            run = session.get(RecipeRun, job.run_id)
            installation = (
                session.get(RecipeInstallation, run.installation_id)
                if run is not None
                else None
            )
            resolved = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if resolved is None:
                return None
            compiled = _compile_contract(
                resolved[1].model_dump(mode="json"), job.interface
            )
            return (
                compiled
                if not isinstance(compiled, Damaged)
                and compiled.sha256() == job.contract_sha256
                else None
            )

        return read_or_rebuild(
            kind="artifact-job.contract",
            subject=job.id,
            read=lambda: CompiledArtifactContract.parse(job.compiled_contract),
            rebuild=rebuild,
        )

    def _view_in_session(self, session: Session, job: ArtifactJob) -> ArtifactJobView:
        submission = _artifact_submission_in_session(session, job)
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        state, actions, cancel_requested_at = adapter.view(job)
        manifest = self._stored_input_manifest(session, job)
        inputs = tuple(
            self._input_mappings(
                self._files_in_session(session, job.id, "input"), manifest
            )
        )
        outputs = tuple(
            self._file_mapping(item)
            for item in self._files_in_session(session, job.id, "output")
        )
        contract = self._stored_contract(session, job)
        if isinstance(contract, Residue) or isinstance(manifest, Residue):
            # The stored contract or the declared inputs are damaged and nothing
            # re-derives them: the job is unreadable, which readers see as not
            # found.
            raise MissingRecord(job.id, reason=InvalidRequestReason.NOT_FOUND)
        view = ArtifactJobView(
            id=job.id,
            run_id=job.run_id,
            # A submission that cannot be read leaves the pair unset together.
            operation_id=job.operation_id if isinstance(submission, Job) else None,
            submit_request_id=(
                submission.request_id if isinstance(submission, Job) else None
            ),
            interface=job.interface,
            state=state,
            preparation=ajs.preparation_of(job),
            cancel_requested_at=cancel_requested_at,
            contract_sha256=job.contract_sha256,
            compiled_contract=contract,
            input_manifest_sha256=job.input_manifest_sha256,
            input_total_bytes=job.input_total_bytes,
            input_declarations=tuple(
                item.model_dump(mode="json") for item in manifest.files
            ),
            input_files=inputs,
            output_limits=dict(job.output_limits),
            output_manifest_sha256=job.output_manifest_sha256,
            output_files=outputs,
            result_evidence=_result_evidence(job.result_evidence),
            status_reason=job.status_reason,
            timeout_seconds=job.timeout_seconds,
            created_at=job.created_at,
            updated_at=job.updated_at,
            supported_actions=actions,
        )
        ArtifactJobResponse.model_validate(view, from_attributes=True)
        return view


__all__ = [
    "MAX_INPUT_FILE_BYTES",
    "ArtifactFileDeclaration",
    "ArtifactJobCapabilitiesResponse",
    "ArtifactJobError",
    "ArtifactJobResponse",
    "ArtifactJobService",
    "ArtifactJobView",
    "CompiledArtifactContract",
    "OutputLimits",
]
