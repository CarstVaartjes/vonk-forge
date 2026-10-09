"""Artifact Jobs: contracts."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, NoReturn

from pydantic import BeforeValidator, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy.orm import Session
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
    RecipeJobOutputMapping,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    canonical_message,
    recipe_job_manifest_sha256,
    state_adopter,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_forge_contracts import RecipeDefinition, read_recipe

from ..artifact_blob_store import ArtifactBlobStoreError, BlobReconciliation
from ..artifact_job_evidence import ArtifactJobResultEvidence
from ..categorized_errors import InvalidValue
from ..compiled_artifact_contract import (
    CompiledArtifactContract,
    ParameterDefinition,
    ParameterScalar,
    compile_artifact_contract,
)
from ..job_documents import RecipeJobRunParent
from ..library_contract import UuidId
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import ArtifactJob, CatalogDocumentRevision, Job
from ..stored_json import read_row_column
from ..strict_json import StrictJSONModel, read_stored_model

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


class StorageReconciliation(BlobReconciliation):
    """One storage reconciliation: expired jobs and blobs, then the store's own."""

    expired_jobs: int = Field(ge=0)
    removed_blob_records: int = Field(ge=0)


class ArtifactJobCapabilitiesResponse(ArtifactJobContractModel):
    transport: ArtifactJobTransportCapabilities
    storage: ArtifactJobStorageCapabilities


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
    compiled_contract: CompiledArtifactContract
    input_manifest_sha256: str
    input_total_bytes: int
    input_declarations: tuple[ArtifactFileDeclaration, ...]
    input_files: tuple[ArtifactFileDeclaration, ...]
    output_limits: OutputLimits
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
    recipe: RecipeDefinition, interface_name: str
) -> CompiledArtifactContract | Damaged:
    """The contract a recipe declares, or ``Damaged`` (with the reason) when it
    does not compile: the caller decides, a request refuses the recipe and a
    rebuild finds no evidence."""

    try:
        return compile_artifact_contract(recipe, interface_name)
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
