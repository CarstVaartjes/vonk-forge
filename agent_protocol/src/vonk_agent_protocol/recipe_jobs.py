"""Closed Pydantic protocol for one-shot artifact-producing recipe jobs."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError, canonical_message
from .wire_model import WireModel

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
MAX_OUTPUT_FILES = 32
MAX_OUTPUT_FILE_BYTES = 1024**3
MAX_OUTPUT_TOTAL_BYTES = 2 * 1024**3
MAX_PARAMETERS_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 60 * 60

_DIGEST = r"^[0-9a-f]{64}$"
_OCI_DIGEST = r"^sha256:[0-9a-f]{64}$"
_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SLOT = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
_EXTENSION = r"^\.[a-z0-9][a-z0-9._-]{0,15}$"
_MEDIA_TYPE = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}$"
)
_UNSAFE_PARAMETER_KEY = re.compile(
    r"^(?:apikey|passwordhash)$|"
    r"(?:^|[_-])(?:password|secret|authorization|command|shell|environment)"
    r"(?:$|[_-])|"
    r"(?:^|[_-])(?:api|access|auth|bearer|github|hf|huggingface)[_-]?token$|"
    r"(?:^|[_-])private[_-]?key$|"
    r"^token$|"
    r"(?:^|[_-])(?:path|file|filename|filepath|directory|folder)(?:$|[_-])",
    re.IGNORECASE,
)

Digest = Annotated[str, StringConstraints(pattern=_DIGEST)]
ImageDigest = Annotated[str, StringConstraints(pattern=_OCI_DIGEST)]
CanonicalUUID = Annotated[str, StringConstraints(pattern=_UUID)]
ArtifactName = Annotated[str, StringConstraints(pattern=_NAME)]
ArtifactSlot = Annotated[str, StringConstraints(pattern=_SLOT)]
ArtifactExtension = Annotated[str, StringConstraints(pattern=_EXTENSION)]
MediaType = Annotated[str, StringConstraints(pattern=_MEDIA_TYPE)]


def _as_tuple(value: object) -> object:
    """Accept JSON arrays while retaining immutable tuples in the model."""
    return tuple(value) if isinstance(value, (list, tuple)) else value


def _thaw(value: object) -> object:
    """Turn the immutable envelope projection back into Pydantic input."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


class _RecipeJobModel(WireModel):
    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _parse_model[ModelT: _RecipeJobModel](
    cls: type[ModelT], raw: Any, *, label: str
) -> ModelT:
    try:
        return cls.model_validate(_thaw(raw))
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(item) for item in first.get("loc", ()))
        message = str(first.get("msg", f"{label} is invalid"))
        raise AgentProtocolError(
            f"{location}: {message}" if location else message
        ) from error


def _validate_parameters(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        raise ValueError("job parameters are too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("job parameter number is not finite")
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value.encode("utf-8")) > 4096:
            raise ValueError("job parameter string is invalid")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise ValueError("job parameter array is too large")
        return [_validate_parameters(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise ValueError("job parameter object is too large")
        result: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key.encode("utf-8")) > 64
                or _UNSAFE_PARAMETER_KEY.search(key)
            ):
                raise ValueError("job parameter key is unsafe")
            result[key] = _validate_parameters(item, depth=depth + 1)
        return result
    raise ValueError("job parameters must contain JSON values")


class RecipeJobFile(_RecipeJobModel):
    name: ArtifactName
    media_type: MediaType
    size_bytes: int = Field(ge=0, le=MAX_OUTPUT_FILE_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def name_is_safe(self) -> RecipeJobFile:
        if self.name == "manifest.json":
            raise ValueError("artifact name is invalid")
        return self

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobFile:
        parsed = _parse_model(cls, raw, label="artifact file")
        if parsed.size_bytes > maximum_bytes:
            raise AgentProtocolError("artifact size_bytes is invalid")
        return parsed


class RecipeJobInputFile(_RecipeJobModel):
    slot: ArtifactSlot
    name: ArtifactName
    media_type: MediaType
    size_bytes: int = Field(ge=0, le=MAX_INPUT_FILE_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def name_is_safe(self) -> RecipeJobInputFile:
        if self.name == "manifest.json":
            raise ValueError("artifact name is invalid")
        return self

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobInputFile:
        parsed = _parse_model(cls, raw, label="artifact input file")
        if parsed.size_bytes > maximum_bytes:
            raise AgentProtocolError("artifact size_bytes is invalid")
        return parsed


def _manifest_document(
    files: Sequence[RecipeJobFile | RecipeJobInputFile],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "total_bytes": sum(item.size_bytes for item in files),
        "files": [item.to_mapping() for item in files],
    }


def manifest_document(
    files: tuple[RecipeJobFile, ...] | tuple[RecipeJobInputFile, ...],
) -> dict[str, object]:
    return _manifest_document(files)


def manifest_sha256(
    files: tuple[RecipeJobFile, ...] | tuple[RecipeJobInputFile, ...],
) -> str:
    return hashlib.sha256(canonical_message(_manifest_document(files))).hexdigest()


class RecipeJobOutputLimits(_RecipeJobModel):
    max_files: int = Field(ge=1, le=MAX_OUTPUT_FILES)
    max_file_bytes: int = Field(ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    allowed_media_types: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("allowed_media_types", mode="before")
    @classmethod
    def array_is_immutable(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("allowed_media_types")
    @classmethod
    def media_types_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or list(value) != sorted(value):
            raise ValueError("allowed output media types are not canonical")
        return value

    @model_validator(mode="after")
    def limits_are_consistent(self) -> RecipeJobOutputLimits:
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("output limits are inconsistent")
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputLimits:
        return _parse_model(cls, raw, label="output limits")


class RecipeJobOutputMapping(_RecipeJobModel):
    slot: ArtifactSlot
    media_type: MediaType
    extensions: Annotated[tuple[ArtifactExtension, ...], BeforeValidator(_as_tuple)] = (
        Field(min_length=1, max_length=16)
    )

    @field_validator("extensions")
    @classmethod
    def extensions_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or list(value) != sorted(value):
            raise ValueError("artifact output extensions are not canonical")
        return value

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputMapping:
        return _parse_model(cls, raw, label="output mapping")


class RecipeJobOutputManifest(_RecipeJobModel):
    schema_version: Literal[1]
    manifest_sha256: Digest
    total_bytes: int = Field(ge=0, le=MAX_OUTPUT_TOTAL_BYTES)
    files: tuple[RecipeJobFile, ...] = Field(max_length=MAX_OUTPUT_FILES)

    @field_validator("files", mode="before")
    @classmethod
    def array_is_immutable(cls, value: object) -> object:
        return _as_tuple(value)

    @model_validator(mode="after")
    def manifest_is_canonical(self) -> RecipeJobOutputManifest:
        names = [item.name for item in self.files]
        if names != sorted(names, key=lambda value: value.encode("utf-8")):
            raise ValueError("artifact manifest is not canonically sorted")
        if len(set(names)) != len(names):
            raise ValueError("artifact manifest limits are exceeded")
        if self.total_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("output manifest digest or size does not match")
        if self.manifest_sha256 != manifest_sha256(self.files):
            raise ValueError("output manifest digest or size does not match")
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputManifest:
        return _parse_model(cls, raw, label="output manifest")


class RecipeJobEvidence(_RecipeJobModel):
    elapsed_milliseconds: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)
    # Required in the wire and nullable when the helper cannot observe a
    # transient container's cgroup peak.
    peak_memory_bytes: int | None = Field(default=..., ge=0, le=16 * 1024**4)


class RecipeJobRunRequest(_RecipeJobModel):
    schema_version: Literal[1]
    job_id: CanonicalUUID
    run_id: CanonicalUUID
    installation_id: CanonicalUUID
    recipe_revision_id: CanonicalUUID
    recipe_content_sha256: Digest
    image_digest: ImageDigest
    plan_digest: Digest
    interface: Literal[
        "audio-job", "video-job", "image-job", "mesh-job", "artifact-job"
    ]
    rank: Literal[0]
    role: Literal["entrypoint"]
    reserved_memory_bytes: int = Field(ge=1, le=16 * 1024**4)
    contract_sha256: Digest
    input_manifest_sha256: Digest
    input_total_bytes: int = Field(ge=0, le=MAX_INPUT_TOTAL_BYTES)
    inputs: tuple[RecipeJobInputFile, ...] = Field(max_length=MAX_INPUT_FILES)
    parameters: dict[str, object]
    output_mappings: tuple[RecipeJobOutputMapping, ...] = Field(
        min_length=1, max_length=MAX_OUTPUT_FILES
    )
    output_limits: RecipeJobOutputLimits
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)

    @field_validator("inputs", "output_mappings", mode="before")
    @classmethod
    def arrays_are_immutable(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("parameters", mode="before")
    @classmethod
    def parameters_are_safe(cls, value: object) -> object:
        parsed = _validate_parameters(value)
        if (
            not isinstance(parsed, dict)
            or len(canonical_message(parsed)) > MAX_PARAMETERS_BYTES
        ):
            raise ValueError("job parameters are invalid")
        return parsed

    @model_validator(mode="after")
    def request_is_canonical(self) -> RecipeJobRunRequest:
        names = [item.name for item in self.inputs]
        if names != sorted(names, key=lambda value: value.encode("utf-8")):
            raise ValueError("artifact manifest is not canonically sorted")
        if len(set(names)) != len(names):
            raise ValueError("artifact manifest limits are exceeded")
        if self.input_total_bytes != sum(item.size_bytes for item in self.inputs):
            raise ValueError("input manifest digest or size does not match")
        if self.input_manifest_sha256 != manifest_sha256(self.inputs):
            raise ValueError("input manifest digest or size does not match")
        slots = [item.slot for item in self.output_mappings]
        extensions = [ext for item in self.output_mappings for ext in item.extensions]
        if slots != sorted(slots, key=lambda value: value.encode("utf-8")):
            raise ValueError("artifact output mappings are not canonical")
        if len(set(slots)) != len(slots) or len(set(extensions)) != len(extensions):
            raise ValueError("artifact output mappings are ambiguous")
        if not set(self.output_limits.allowed_media_types) <= {
            item.media_type for item in self.output_mappings
        }:
            raise ValueError("allowed output media types lack a mapping")
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunRequest:
        return _parse_model(cls, raw, label="recipe job request")


class RecipeJobRunResult(_RecipeJobModel):
    schema_version: Literal[1]
    job_id: CanonicalUUID
    run_id: CanonicalUUID
    exit_code: int = Field(ge=0, le=255)
    output_manifest: RecipeJobOutputManifest
    evidence: RecipeJobEvidence
    # Rust uses skip_serializing_if for this optional nullable field. Both an
    # omitted field and explicit JSON null are accepted on input.
    reason: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def reason_is_safe(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("recipe job failure reason is invalid")
        return value

    @property
    def outputs(self) -> tuple[RecipeJobFile, ...]:
        return self.output_manifest.files

    @property
    def output_manifest_sha256(self) -> str:
        return self.output_manifest.manifest_sha256

    @property
    def elapsed_milliseconds(self) -> int:
        return self.evidence.elapsed_milliseconds

    @property
    def peak_memory_bytes(self) -> int | None:
        return self.evidence.peak_memory_bytes

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunResult:
        return _parse_model(cls, raw, label="recipe job result")

    def to_mapping(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        if self.reason is None:
            value.pop("reason", None)
        return value


__all__ = [
    "MAX_INPUT_FILES",
    "MAX_INPUT_FILE_BYTES",
    "MAX_INPUT_TOTAL_BYTES",
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_TOTAL_BYTES",
    "RecipeJobEvidence",
    "RecipeJobFile",
    "RecipeJobInputFile",
    "RecipeJobOutputLimits",
    "RecipeJobOutputManifest",
    "RecipeJobOutputMapping",
    "RecipeJobRunRequest",
    "RecipeJobRunResult",
    "manifest_document",
    "manifest_sha256",
]
