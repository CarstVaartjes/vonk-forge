"""Closed protocol for one-shot artifact-producing recipe jobs."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import (
    AgentProtocolError,
    _fields,
    _mapping,
    _uuid,
    _version,
    canonical_message,
)
from .wire_model import WireModel

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
MAX_OUTPUT_FILES = 32
MAX_OUTPUT_FILE_BYTES = 1024**3
MAX_OUTPUT_TOTAL_BYTES = 2 * 1024**3
MAX_PARAMETERS_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 60 * 60

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SLOT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_EXTENSION = re.compile(r"\.[a-z0-9][a-z0-9._-]{0,15}\Z")
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z"
)
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_INTERFACES = frozenset(
    {"audio-job", "video-job", "image-job", "mesh-job", "artifact-job"}
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


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


def _bounded_int(value: object, name: str, *, minimum: int = 0, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise AgentProtocolError(f"{name} is invalid")
    return value


def _name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _NAME.fullmatch(value) is None
        or value in {".", "..", "manifest.json"}
        or len(value.encode("utf-8")) > 128
    ):
        raise AgentProtocolError("artifact name is invalid")
    return value


def _slot(value: object) -> str:
    if not isinstance(value, str) or _SLOT.fullmatch(value) is None:
        raise AgentProtocolError("artifact input slot is invalid")
    return value


def _media_type(value: object) -> str:
    if not isinstance(value, str) or _MEDIA_TYPE.fullmatch(value) is None:
        raise AgentProtocolError("artifact media type is invalid")
    return value


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobFile:
    name: str
    media_type: str
    size_bytes: int
    sha256: str

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobFile:
        value = _mapping(raw)
        _fields(value, required={"name", "media_type", "size_bytes", "sha256"})
        return cls(
            name=_name(value["name"]),
            media_type=_media_type(value["media_type"]),
            size_bytes=_bounded_int(
                value["size_bytes"],
                "artifact size_bytes",
                maximum=maximum_bytes,
            ),
            sha256=_digest(value["sha256"], "artifact sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobInputFile:
    slot: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobInputFile:
        value = _mapping(raw)
        _fields(
            value,
            required={"slot", "name", "media_type", "size_bytes", "sha256"},
        )
        return cls(
            slot=_slot(value["slot"]),
            name=_name(value["name"]),
            media_type=_media_type(value["media_type"]),
            size_bytes=_bounded_int(
                value["size_bytes"],
                "artifact size_bytes",
                maximum=maximum_bytes,
            ),
            sha256=_digest(value["sha256"], "artifact sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def manifest_document(
    files: tuple[RecipeJobFile, ...] | tuple[RecipeJobInputFile, ...],
) -> dict[str, object]:
    total = sum(item.size_bytes for item in files)
    return {
        "schema_version": 1,
        "total_bytes": total,
        "files": [item.to_mapping() for item in files],
    }


def manifest_sha256(
    files: tuple[RecipeJobFile, ...] | tuple[RecipeJobInputFile, ...],
) -> str:
    return hashlib.sha256(canonical_message(manifest_document(files))).hexdigest()


def _files(
    raw: object,
    *,
    maximum_count: int,
    maximum_file_bytes: int,
    maximum_total_bytes: int,
) -> tuple[RecipeJobFile, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > maximum_count:
        raise AgentProtocolError("artifact manifest file count is invalid")
    parsed = tuple(
        RecipeJobFile.parse(item, maximum_bytes=maximum_file_bytes) for item in raw
    )
    names = [item.name for item in parsed]
    if names != sorted(names, key=lambda value: value.encode("utf-8")):
        raise AgentProtocolError("artifact manifest is not canonically sorted")
    if (
        len(set(names)) != len(names)
        or sum(item.size_bytes for item in parsed) > maximum_total_bytes
    ):
        raise AgentProtocolError("artifact manifest limits are exceeded")
    return parsed


def _input_files(
    raw: object,
    *,
    maximum_count: int,
    maximum_file_bytes: int,
    maximum_total_bytes: int,
) -> tuple[RecipeJobInputFile, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > maximum_count:
        raise AgentProtocolError("artifact manifest file count is invalid")
    parsed = tuple(
        RecipeJobInputFile.parse(item, maximum_bytes=maximum_file_bytes) for item in raw
    )
    names = [item.name for item in parsed]
    if names != sorted(names, key=lambda value: value.encode("utf-8")):
        raise AgentProtocolError("artifact manifest is not canonically sorted")
    if (
        len(set(names)) != len(names)
        or sum(item.size_bytes for item in parsed) > maximum_total_bytes
    ):
        raise AgentProtocolError("artifact manifest limits are exceeded")
    return parsed


def _parameters(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        raise AgentProtocolError("job parameters are too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise AgentProtocolError("job parameter number is not finite")
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value.encode("utf-8")) > 4096:
            raise AgentProtocolError("job parameter string is invalid")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise AgentProtocolError("job parameter array is too large")
        return tuple(_parameters(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise AgentProtocolError("job parameter object is too large")
        projected: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key.encode("utf-8")) > 64
                or _UNSAFE_PARAMETER_KEY.search(key)
            ):
                raise AgentProtocolError("job parameter key is unsafe")
            projected[key] = _parameters(item, depth=depth + 1)
        return MappingProxyType(projected)
    raise AgentProtocolError("job parameters must contain JSON values")


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobOutputLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    allowed_media_types: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputLimits:
        value = _mapping(raw)
        _fields(
            value,
            required={
                "max_files",
                "max_file_bytes",
                "max_total_bytes",
                "allowed_media_types",
            },
        )
        allowed = value["allowed_media_types"]
        if not isinstance(allowed, (list, tuple)) or not 1 <= len(allowed) <= 16:
            raise AgentProtocolError("allowed output media types are invalid")
        media_types = tuple(_media_type(item) for item in allowed)
        if len(set(media_types)) != len(media_types) or list(media_types) != sorted(
            media_types
        ):
            raise AgentProtocolError("allowed output media types are not canonical")
        max_files = _bounded_int(
            value["max_files"], "max_files", minimum=1, maximum=MAX_OUTPUT_FILES
        )
        max_file_bytes = _bounded_int(
            value["max_file_bytes"],
            "max_file_bytes",
            minimum=1,
            maximum=MAX_OUTPUT_FILE_BYTES,
        )
        max_total_bytes = _bounded_int(
            value["max_total_bytes"],
            "max_total_bytes",
            minimum=1,
            maximum=MAX_OUTPUT_TOTAL_BYTES,
        )
        if max_file_bytes > max_total_bytes:
            raise AgentProtocolError("output limits are inconsistent")
        return cls(max_files, max_file_bytes, max_total_bytes, media_types)

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "allowed_media_types": list(self.allowed_media_types),
        }


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobOutputMapping:
    slot: str
    media_type: str
    extensions: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputMapping:
        value = _mapping(raw)
        _fields(value, required={"slot", "media_type", "extensions"})
        extensions = value["extensions"]
        if not isinstance(extensions, (list, tuple)) or not 1 <= len(extensions) <= 16:
            raise AgentProtocolError("artifact output extensions are invalid")
        parsed_extensions = tuple(extensions)
        if (
            any(
                not isinstance(item, str) or _EXTENSION.fullmatch(item) is None
                for item in parsed_extensions
            )
            or len(set(parsed_extensions)) != len(parsed_extensions)
            or list(parsed_extensions) != sorted(parsed_extensions)
        ):
            raise AgentProtocolError("artifact output extensions are not canonical")
        return cls(
            slot=_slot(value["slot"]),
            media_type=_media_type(value["media_type"]),
            extensions=parsed_extensions,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "media_type": self.media_type,
            "extensions": list(self.extensions),
        }


def _output_mappings(raw: object) -> tuple[RecipeJobOutputMapping, ...]:
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_OUTPUT_FILES:
        raise AgentProtocolError("artifact output mappings are invalid")
    mappings = tuple(RecipeJobOutputMapping.parse(item) for item in raw)
    slots = [item.slot for item in mappings]
    extensions = [extension for item in mappings for extension in item.extensions]
    if slots != sorted(slots, key=lambda value: value.encode("utf-8")):
        raise AgentProtocolError("artifact output mappings are not canonical")
    if len(set(slots)) != len(slots) or len(set(extensions)) != len(extensions):
        raise AgentProtocolError("artifact output mappings are ambiguous")
    return mappings


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobRunRequest:
    schema_version: int
    job_id: str
    run_id: str
    installation_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    image_digest: str
    plan_digest: str
    interface: str
    rank: int
    role: str
    reserved_memory_bytes: int
    contract_sha256: str
    input_manifest_sha256: str
    input_total_bytes: int
    inputs: tuple[RecipeJobInputFile, ...]
    parameters: Mapping[str, object]
    output_mappings: tuple[RecipeJobOutputMapping, ...]
    output_limits: RecipeJobOutputLimits
    timeout_seconds: int

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunRequest:
        value = _mapping(raw)
        _fields(
            value, required={field.name for field in cls.__dataclass_fields__.values()}
        )
        inputs = _input_files(
            value["inputs"],
            maximum_count=MAX_INPUT_FILES,
            maximum_file_bytes=MAX_INPUT_FILE_BYTES,
            maximum_total_bytes=MAX_INPUT_TOTAL_BYTES,
        )
        total = sum(item.size_bytes for item in inputs)
        if value["input_total_bytes"] != total or value[
            "input_manifest_sha256"
        ] != manifest_sha256(inputs):
            raise AgentProtocolError("input manifest digest or size does not match")
        parameters = _parameters(value["parameters"])
        if (
            not isinstance(parameters, Mapping)
            or len(canonical_message(parameters)) > MAX_PARAMETERS_BYTES
        ):
            raise AgentProtocolError("job parameters are invalid")
        interface = value["interface"]
        if interface not in _INTERFACES:
            raise AgentProtocolError("recipe job interface is invalid")
        image_digest = value["image_digest"]
        if (
            not isinstance(image_digest, str)
            or _OCI_DIGEST.fullmatch(image_digest) is None
        ):
            raise AgentProtocolError("image digest is invalid")
        role = value["role"]
        if not isinstance(role, str) or _ROLE.fullmatch(role) is None:
            raise AgentProtocolError("recipe job role is invalid")
        output_mappings = _output_mappings(value["output_mappings"])
        output_limits = RecipeJobOutputLimits.parse(value["output_limits"])
        if not set(output_limits.allowed_media_types) <= {
            item.media_type for item in output_mappings
        }:
            raise AgentProtocolError("allowed output media types lack a mapping")
        return cls(
            schema_version=_version(value["schema_version"]),
            job_id=_uuid(value["job_id"], name="job_id"),
            run_id=_uuid(value["run_id"], name="run_id"),
            installation_id=_uuid(value["installation_id"], name="installation_id"),
            recipe_revision_id=_uuid(
                value["recipe_revision_id"], name="recipe_revision_id"
            ),
            recipe_content_sha256=_digest(
                value["recipe_content_sha256"], "recipe_content_sha256"
            ),
            image_digest=image_digest,
            plan_digest=_digest(value["plan_digest"], "plan_digest"),
            interface=interface,
            rank=_bounded_int(value["rank"], "rank", maximum=2**32 - 1),
            role=role,
            reserved_memory_bytes=_bounded_int(
                value["reserved_memory_bytes"],
                "reserved_memory_bytes",
                minimum=1,
                maximum=16 * 1024**4,
            ),
            contract_sha256=_digest(value["contract_sha256"], "contract_sha256"),
            input_manifest_sha256=value["input_manifest_sha256"],
            input_total_bytes=total,
            inputs=inputs,
            parameters=parameters,
            output_mappings=output_mappings,
            output_limits=output_limits,
            timeout_seconds=_bounded_int(
                value["timeout_seconds"],
                "timeout_seconds",
                minimum=1,
                maximum=MAX_TIMEOUT_SECONDS,
            ),
        )


@dataclass(frozen=True, slots=True)
class _LegacyRecipeJobRunResult:
    schema_version: int
    job_id: str
    run_id: str
    exit_code: int
    output_manifest_sha256: str
    outputs: tuple[RecipeJobFile, ...]
    elapsed_milliseconds: int
    peak_memory_bytes: int | None
    reason: str | None

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunResult:
        value = _mapping(raw)
        required = {
            "schema_version",
            "job_id",
            "run_id",
            "exit_code",
            "output_manifest",
            "evidence",
        }
        optional = {"reason"}
        if not required <= set(value) or set(value) - required - optional:
            raise AgentProtocolError("recipe job result fields are invalid")
        manifest = _mapping(value["output_manifest"])
        _fields(
            manifest,
            required={"schema_version", "manifest_sha256", "total_bytes", "files"},
        )
        if _version(manifest["schema_version"]) != 1:
            raise AgentProtocolError("output manifest version is invalid")
        outputs = _files(
            manifest["files"],
            maximum_count=MAX_OUTPUT_FILES,
            maximum_file_bytes=MAX_OUTPUT_FILE_BYTES,
            maximum_total_bytes=MAX_OUTPUT_TOTAL_BYTES,
        )
        if manifest["total_bytes"] != sum(
            item.size_bytes for item in outputs
        ) or manifest["manifest_sha256"] != manifest_sha256(outputs):
            raise AgentProtocolError("output manifest digest or size does not match")
        evidence = _mapping(value["evidence"])
        _fields(evidence, required={"elapsed_milliseconds", "peak_memory_bytes"})
        reason = value.get("reason")
        if reason is not None and (
            not isinstance(reason, str) or not 1 <= len(reason) <= 512
        ):
            raise AgentProtocolError("recipe job failure reason is invalid")
        return cls(
            schema_version=_version(value["schema_version"]),
            job_id=_uuid(value["job_id"], name="job_id"),
            run_id=_uuid(value["run_id"], name="run_id"),
            exit_code=_bounded_int(value["exit_code"], "exit_code", maximum=255),
            output_manifest_sha256=manifest["manifest_sha256"],
            outputs=outputs,
            elapsed_milliseconds=_bounded_int(
                evidence["elapsed_milliseconds"],
                "elapsed_milliseconds",
                maximum=7 * 24 * 60 * 60 * 1000,
            ),
            peak_memory_bytes=(
                None
                if evidence["peak_memory_bytes"] is None
                else _bounded_int(
                    evidence["peak_memory_bytes"],
                    "peak_memory_bytes",
                    maximum=16 * 1024**4,
                )
            ),
            reason=reason,
        )


class _RecipeWireModel(WireModel):
    """Pydantic base for strict recipe job payloads with legacy error wording."""

    @classmethod
    def _parse(cls, raw: Any, **kwargs: Any) -> Any:
        try:
            return cls.model_validate(raw, **kwargs)
        except AgentProtocolError:
            raise
        except ValidationError as error:
            raise AgentProtocolError(str(error)) from error


class RecipeJobFile(_RecipeWireModel):
    name: str
    media_type: str
    size_bytes: int = Field(strict=True, ge=0, le=MAX_OUTPUT_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_file(self) -> RecipeJobFile:
        _name(self.name)
        _media_type(self.media_type)
        _digest(self.sha256, "artifact sha256")
        return self

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobFile:
        value = _mapping(raw)
        _fields(value, required={"name", "media_type", "size_bytes", "sha256"})
        parsed = cls._parse(value)
        if parsed.size_bytes > maximum_bytes:
            raise AgentProtocolError("artifact size_bytes is invalid")
        return parsed

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class RecipeJobInputFile(_RecipeWireModel):
    slot: str
    name: str
    media_type: str
    size_bytes: int = Field(strict=True, ge=0, le=MAX_INPUT_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_file(self) -> RecipeJobInputFile:
        _slot(self.slot)
        _name(self.name)
        _media_type(self.media_type)
        _digest(self.sha256, "artifact sha256")
        return self

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobInputFile:
        value = _mapping(raw)
        _fields(value, required={"slot", "name", "media_type", "size_bytes", "sha256"})
        parsed = cls._parse(value)
        if parsed.size_bytes > maximum_bytes:
            raise AgentProtocolError("artifact size_bytes is invalid")
        return parsed

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class RecipeJobOutputLimits(_RecipeWireModel):
    max_files: int = Field(strict=True, ge=1, le=MAX_OUTPUT_FILES)
    max_file_bytes: int = Field(strict=True, ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_total_bytes: int = Field(strict=True, ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    allowed_media_types: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def lists_are_sequences(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and isinstance(value.get("allowed_media_types"), list):
            return {**value, "allowed_media_types": tuple(value["allowed_media_types"])}
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> RecipeJobOutputLimits:
        media_types = tuple(_media_type(item) for item in self.allowed_media_types)
        if len(set(media_types)) != len(media_types) or list(media_types) != sorted(media_types):
            raise ValueError("allowed output media types are not canonical")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("output limits are inconsistent")
        object.__setattr__(self, "allowed_media_types", media_types)
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputLimits:
        value = _mapping(raw)
        _fields(value, required={"max_files", "max_file_bytes", "max_total_bytes", "allowed_media_types"})
        return cls._parse(value)

    def to_mapping(self) -> dict[str, object]:
        return {**self.model_dump(mode="python"), "allowed_media_types": list(self.allowed_media_types)}


class RecipeJobOutputMapping(_RecipeWireModel):
    slot: str
    media_type: str
    extensions: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def lists_are_sequences(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and isinstance(value.get("extensions"), list):
            return {**value, "extensions": tuple(value["extensions"])}
        return value

    @model_validator(mode="after")
    def validate_mapping(self) -> RecipeJobOutputMapping:
        _slot(self.slot)
        _media_type(self.media_type)
        if any(_EXTENSION.fullmatch(item) is None for item in self.extensions):
            raise ValueError("artifact output extensions are not canonical")
        if len(set(self.extensions)) != len(self.extensions) or list(self.extensions) != sorted(self.extensions):
            raise ValueError("artifact output extensions are not canonical")
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobOutputMapping:
        value = _mapping(raw)
        _fields(value, required={"slot", "media_type", "extensions"})
        return cls._parse(value)

    def to_mapping(self) -> dict[str, object]:
        return {**self.model_dump(mode="python"), "extensions": list(self.extensions)}


class RecipeJobRunRequest(_RecipeWireModel):
    schema_version: Literal[1]
    job_id: str
    run_id: str
    installation_id: str
    recipe_revision_id: str
    recipe_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    interface: str
    rank: int = Field(strict=True, ge=0, le=2**32 - 1)
    role: str
    reserved_memory_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_total_bytes: int = Field(strict=True, ge=0, le=MAX_INPUT_TOTAL_BYTES)
    inputs: tuple[RecipeJobInputFile, ...] = Field(max_length=MAX_INPUT_FILES)
    parameters: dict[str, Any]
    output_mappings: tuple[RecipeJobOutputMapping, ...] = Field(min_length=1, max_length=MAX_OUTPUT_FILES)
    output_limits: RecipeJobOutputLimits
    timeout_seconds: int = Field(strict=True, ge=1, le=MAX_TIMEOUT_SECONDS)

    @model_validator(mode="before")
    @classmethod
    def lists_are_sequences(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        document = dict(value)
        for name in ("inputs", "output_mappings"):
            if isinstance(document.get(name), list):
                document[name] = tuple(document[name])
        limits = document.get("output_limits")
        if isinstance(limits, Mapping) and isinstance(limits.get("allowed_media_types"), list):
            document["output_limits"] = {**limits, "allowed_media_types": tuple(limits["allowed_media_types"])}
        return document

    @model_validator(mode="after")
    def validate_request(self) -> RecipeJobRunRequest:
        for name in ("job_id", "run_id", "installation_id", "recipe_revision_id"):
            _uuid(getattr(self, name), name=name)
        if self.interface not in _INTERFACES:
            raise ValueError("recipe job interface is invalid")
        if _ROLE.fullmatch(self.role) is None:
            raise ValueError("recipe job role is invalid")
        inputs = _input_files([item.model_dump(mode="python") for item in self.inputs], maximum_count=MAX_INPUT_FILES, maximum_file_bytes=MAX_INPUT_FILE_BYTES, maximum_total_bytes=MAX_INPUT_TOTAL_BYTES)
        total = sum(item.size_bytes for item in inputs)
        if self.input_total_bytes != total or self.input_manifest_sha256 != manifest_sha256(inputs):
            raise ValueError("input manifest digest or size does not match")
        parameters = _parameters(self.parameters)
        if not isinstance(parameters, Mapping) or len(canonical_message(parameters)) > MAX_PARAMETERS_BYTES:
            raise ValueError("job parameters are invalid")
        mappings = _output_mappings([item.model_dump(mode="python") for item in self.output_mappings])
        if not set(self.output_limits.allowed_media_types) <= {item.media_type for item in mappings}:
            raise ValueError("allowed output media types lack a mapping")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "parameters", dict(parameters))
        object.__setattr__(self, "output_mappings", mappings)
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunRequest:
        value = _mapping(raw)
        _fields(value, required=set(cls.model_fields))
        return cls._parse(value)


class RecipeJobOutputManifest(_RecipeWireModel):
    schema_version: Literal[1]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_bytes: int = Field(strict=True, ge=0, le=MAX_OUTPUT_TOTAL_BYTES)
    files: tuple[RecipeJobFile, ...] = Field(max_length=MAX_OUTPUT_FILES)

    @model_validator(mode="before")
    @classmethod
    def lists_are_sequences(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and isinstance(value.get("files"), list):
            return {**value, "files": tuple(value["files"])}
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> RecipeJobOutputManifest:
        files = _files([item.model_dump(mode="python") for item in self.files], maximum_count=MAX_OUTPUT_FILES, maximum_file_bytes=MAX_OUTPUT_FILE_BYTES, maximum_total_bytes=MAX_OUTPUT_TOTAL_BYTES)
        if self.total_bytes != sum(item.size_bytes for item in files) or self.manifest_sha256 != manifest_sha256(files):
            raise ValueError("output manifest digest or size does not match")
        object.__setattr__(self, "files", files)
        return self


class RecipeJobEvidence(_RecipeWireModel):
    elapsed_milliseconds: int = Field(strict=True, ge=0, le=7 * 24 * 60 * 60 * 1000)
    peak_memory_bytes: int | None = Field(strict=True, ge=0, le=16 * 1024**4)


class RecipeJobRunResult(_RecipeWireModel):
    schema_version: Literal[1]
    job_id: str
    run_id: str
    exit_code: int = Field(strict=True, ge=0, le=255)
    output_manifest: RecipeJobOutputManifest
    evidence: RecipeJobEvidence
    reason: str | None = None

    @property
    def output_manifest_sha256(self) -> str:
        return self.output_manifest.manifest_sha256

    @property
    def outputs(self) -> tuple[RecipeJobFile, ...]:
        return self.output_manifest.files

    @property
    def elapsed_milliseconds(self) -> int:
        return self.evidence.elapsed_milliseconds

    @property
    def peak_memory_bytes(self) -> int | None:
        return self.evidence.peak_memory_bytes

    @model_validator(mode="after")
    def validate_result(self) -> RecipeJobRunResult:
        _uuid(self.job_id, name="job_id")
        _uuid(self.run_id, name="run_id")
        if self.reason is not None and not 1 <= len(self.reason) <= 512:
            raise ValueError("recipe job failure reason is invalid")
        return self

    @classmethod
    def parse(cls, raw: Any) -> RecipeJobRunResult:
        value = _mapping(raw)
        required = {"schema_version", "job_id", "run_id", "exit_code", "output_manifest", "evidence"}
        if not required <= set(value) or set(value) - required - {"reason"}:
            raise AgentProtocolError("recipe job result fields are invalid")
        return cls._parse(value)


__all__ = [
    "MAX_INPUT_FILES",
    "MAX_INPUT_FILE_BYTES",
    "MAX_INPUT_TOTAL_BYTES",
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_TOTAL_BYTES",
    "RecipeJobFile",
    "RecipeJobInputFile",
    "RecipeJobOutputLimits",
    "RecipeJobOutputMapping",
    "RecipeJobRunRequest",
    "RecipeJobRunResult",
    "manifest_document",
    "manifest_sha256",
]
