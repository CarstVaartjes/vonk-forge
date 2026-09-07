"""Canonical compiled contract for artifact-producing recipe jobs.

The recipe document is the authoring contract.  This module is the one typed
projection used after compilation, while ``engine`` remains an opaque JSON
extension point for engine-specific options.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from vonk_agent_protocol import canonical_message

from .strict_json import StrictJSONModel

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
MAX_OUTPUT_FILES = 32
MAX_OUTPUT_FILE_BYTES = 1024**3
MAX_OUTPUT_TOTAL_BYTES = 2 * 1024**3
MAX_PARAMETERS = 64

_SLOT = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
_EXTENSION = r"^\.[a-z0-9][a-z0-9._-]{0,15}$"
_MEDIA = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
_PARAMETER = r"^[a-z][a-z0-9_-]{0,63}$"
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

SlotId = Annotated[str, StringConstraints(pattern=_SLOT)]
Extension = Annotated[str, StringConstraints(pattern=_EXTENSION)]
MediaType = Annotated[str, StringConstraints(pattern=_MEDIA)]
ParameterName = Annotated[str, StringConstraints(pattern=_PARAMETER)]
type ParameterScalar = bool | int | float | str


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, (list, tuple)) else value


def _finite(value: object) -> bool:
    return (isinstance(value, float) and math.isfinite(value)) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


class ArtifactContractModel(StrictJSONModel):
    """Strict JSON object used by the compiled artifact contract."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ArtifactSlotContract(ArtifactContractModel):
    id: SlotId
    label: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=256)
    media_types: tuple[MediaType, ...] = Field(min_length=1, max_length=16)
    extensions: tuple[Extension, ...] = Field(max_length=16)
    min_files: int = Field(ge=0, le=MAX_INPUT_FILES)
    max_files: int = Field(ge=1, le=MAX_INPUT_FILES)
    max_file_bytes: int = Field(ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)

    @field_validator("media_types", "extensions", mode="before")
    @classmethod
    def arrays_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("media_types", "extensions")
    @classmethod
    def arrays_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or list(value) != sorted(value):
            raise ValueError("artifact slot arrays are not canonical")
        return value

    @model_validator(mode="after")
    def limits_are_consistent(self) -> ArtifactSlotContract:
        if (
            self.min_files > self.max_files
            or self.max_file_bytes > self.max_total_bytes
        ):
            raise ValueError("artifact slot limits are inconsistent")
        return self


class ArtifactInputContract(ArtifactContractModel):
    required: bool
    media_types: tuple[MediaType, ...] = Field(max_length=16)
    max_bytes: int = Field(ge=0, le=MAX_INPUT_TOTAL_BYTES)
    slots: tuple[ArtifactSlotContract, ...] = Field(max_length=MAX_INPUT_FILES)

    @field_validator("media_types", "slots", mode="before")
    @classmethod
    def arrays_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("media_types")
    @classmethod
    def media_types_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or list(value) != sorted(value):
            raise ValueError("artifact input media types are not canonical")
        return value

    @model_validator(mode="after")
    def slots_are_consistent(self) -> ArtifactInputContract:
        if self.media_types and not self.slots:
            raise ValueError("artifact input slot contract is invalid")
        ids = [item.id for item in self.slots]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("artifact input slot ids are not canonical")
        aggregate = set(self.media_types)
        for slot in self.slots:
            if not set(slot.media_types) <= aggregate:
                raise ValueError("artifact input slot media type is undeclared")
            if (
                slot.max_file_bytes > MAX_INPUT_FILE_BYTES
                or slot.max_total_bytes > self.max_bytes
            ):
                raise ValueError("artifact input slot exceeds aggregate limit")
        if self.required and not any(item.min_files > 0 for item in self.slots):
            raise ValueError("required artifact input has no required slot")
        return self


class ArtifactOutputContract(ArtifactContractModel):
    path: Literal["/outputs"]
    max_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    slots: tuple[ArtifactSlotContract, ...] = Field(
        min_length=1, max_length=MAX_OUTPUT_FILES
    )

    @field_validator("slots", mode="before")
    @classmethod
    def slots_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def slots_are_consistent(self) -> ArtifactOutputContract:
        ids = [item.id for item in self.slots]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("artifact output slot ids are not canonical")
        extensions = [extension for slot in self.slots for extension in slot.extensions]
        if len(extensions) != len(set(extensions)):
            raise ValueError("artifact output extensions are ambiguous")
        for slot in self.slots:
            if len(slot.media_types) != 1 or not slot.extensions:
                raise ValueError("artifact output slot contract is invalid")
            if slot.max_total_bytes > self.max_total_bytes:
                raise ValueError("artifact output slot exceeds aggregate limit")
            if slot.max_file_bytes > MAX_OUTPUT_FILE_BYTES:
                raise ValueError("artifact output slot file limit is invalid")
        return self


class ArtifactOutputLimits(ArtifactContractModel):
    max_files: int = Field(ge=1, le=MAX_OUTPUT_FILES)
    max_file_bytes: int = Field(ge=1, le=MAX_OUTPUT_FILE_BYTES)
    max_total_bytes: int = Field(ge=1, le=MAX_OUTPUT_TOTAL_BYTES)
    allowed_media_types: tuple[MediaType, ...] = Field(min_length=1, max_length=16)

    @field_validator("allowed_media_types", mode="before")
    @classmethod
    def media_types_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("allowed_media_types")
    @classmethod
    def media_types_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or list(value) != sorted(value):
            raise ValueError("allowed output media types are not canonical")
        return value

    @model_validator(mode="after")
    def limits_are_consistent(self) -> ArtifactOutputLimits:
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("output limits are inconsistent")
        return self


class _ArtifactParameterBase(ArtifactContractModel):
    name: ParameterName
    minimum: int | float | None = None
    maximum: int | float | None = None
    allowed_values: tuple[ParameterScalar, ...] = Field(
        default_factory=tuple, max_length=128
    )
    pattern: str | None = Field(default=None, max_length=256)

    @field_validator("allowed_values", mode="before")
    @classmethod
    def allowed_values_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("pattern")
    @classmethod
    def pattern_is_safe(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("artifact parameter pattern is invalid")
        return value

    @model_validator(mode="after")
    def name_is_safe(self):
        if _UNSAFE_PARAMETER_KEY.search(self.name):
            raise ValueError("artifact parameter name is unsafe")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("artifact parameter range is invalid")
        if len(self.allowed_values) and len(set(self.allowed_values)) != len(
            self.allowed_values
        ):
            raise ValueError("artifact parameter allowed values are not unique")
        return self


class StringParameter(_ArtifactParameterBase):
    type: Literal["string"]
    default: str
    minimum: None = None
    maximum: None = None

    @field_validator("default")
    @classmethod
    def default_is_safe(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > 4096:
            raise ValueError("artifact parameter default is invalid")
        return value


class IntegerParameter(_ArtifactParameterBase):
    type: Literal["integer"]
    default: int
    minimum: int | None = None
    maximum: int | None = None


class FloatParameter(_ArtifactParameterBase):
    type: Literal["float"]
    default: float | int
    minimum: float | int | None = None
    maximum: float | int | None = None

    @field_validator("default", "minimum", "maximum")
    @classmethod
    def numbers_are_finite(cls, value):
        if value is not None and not _finite(value):
            raise ValueError("artifact parameter number is not finite")
        return value


class BooleanParameter(_ArtifactParameterBase):
    type: Literal["boolean"]
    default: bool
    minimum: None = None
    maximum: None = None


class EnumParameter(_ArtifactParameterBase):
    type: Literal["enum"]
    default: ParameterScalar
    minimum: None = None
    maximum: None = None
    allowed_values: tuple[ParameterScalar, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def default_is_allowed(self) -> EnumParameter:
        if self.default not in self.allowed_values:
            raise ValueError("artifact enum default is not allowed")
        return self


type ParameterDefinition = Annotated[
    StringParameter
    | IntegerParameter
    | FloatParameter
    | BooleanParameter
    | EnumParameter,
    Field(discriminator="type"),
]

_PARAMETER_ADAPTER = TypeAdapter(ParameterDefinition)


def validate_parameter_definition(raw: Mapping[str, object]) -> ParameterDefinition:
    """Validate one parameter declaration with the contract's discriminator."""
    return _PARAMETER_ADAPTER.validate_python(raw)


class CompiledArtifactContract(ArtifactContractModel):
    """The canonical typed artifact execution contract."""

    schema_version: Literal[1]
    interface: Literal[
        "audio-job", "video-job", "image-job", "mesh-job", "artifact-job"
    ]
    input: ArtifactInputContract
    parameters: tuple[ParameterDefinition, ...] = Field(max_length=MAX_PARAMETERS)
    output: ArtifactOutputContract
    output_limits: ArtifactOutputLimits
    max_timeout_seconds: int = Field(ge=1, le=3_600)
    engine: dict[str, object] | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def parameters_are_immutable(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def parameters_are_canonical(self) -> CompiledArtifactContract:
        names = [item.name for item in self.parameters]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("artifact parameters are not canonical")
        return self

    @model_serializer(mode="wrap")
    def serialize_without_empty_engine(self, handler: Any):
        document = handler(self)
        if document.get("engine") is None:
            document.pop("engine", None)
        return document

    @classmethod
    def parse(cls, raw: object) -> CompiledArtifactContract:
        try:
            return cls.model_validate(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("compiled artifact contract is invalid") from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def sha256(self) -> str:
        return hashlib.sha256(canonical_message(self.to_mapping())).hexdigest()


def _slot(raw: Mapping[str, object]) -> dict[str, object]:
    """Copy recipe slot values into a model-owned document."""
    if not isinstance(raw, Mapping):
        raise TypeError("artifact slot contract is invalid")
    extensions = raw.get("extensions")
    media_types = raw.get("media_types")
    return {
        "id": raw.get("id"),
        "label": raw.get("label"),
        "description": raw.get("description"),
        "media_types": sorted(media_types)
        if isinstance(media_types, list)
        else media_types,
        "extensions": sorted(extensions)
        if isinstance(extensions, list)
        else extensions,
        "min_files": raw.get("min_files"),
        "max_files": raw.get("max_files"),
        "max_file_bytes": raw.get("max_file_bytes"),
        "max_total_bytes": raw.get("max_total_bytes"),
    }


def compile_artifact_contract(
    document: Mapping[str, object], interface_name: str
) -> CompiledArtifactContract:
    interfaces = document.get("interfaces")
    interface = (
        next(
            (
                item
                for item in interfaces
                if isinstance(item, Mapping) and item.get("adapter") == interface_name
            ),
            None,
        )
        if isinstance(interfaces, list)
        else None
    )
    if not isinstance(interface, Mapping):
        raise TypeError("artifact job interface contract is unavailable")

    raw_input = interface.get("input")
    if raw_input is None:
        input_document: dict[str, object] = {
            "required": False,
            "media_types": [],
            "max_bytes": 0,
            "slots": [],
        }
    elif isinstance(raw_input, Mapping):
        media_types = raw_input.get("media_types")
        max_bytes = raw_input.get("max_bytes")
        required = raw_input.get("required")
        raw_slots = raw_input.get("slots")
        if (
            raw_slots is None
            and isinstance(media_types, list)
            and isinstance(max_bytes, int)
            and isinstance(required, bool)
        ):
            raw_slots = [
                {
                    "id": "input",
                    "label": "Input",
                    "description": "Recipe input",
                    "media_types": media_types,
                    "extensions": [],
                    "min_files": 1 if required else 0,
                    "max_files": MAX_INPUT_FILES,
                    "max_file_bytes": min(max_bytes, MAX_INPUT_FILE_BYTES),
                    "max_total_bytes": max_bytes,
                }
            ]
        input_document = {
            "required": required,
            "media_types": sorted(set(media_types))
            if isinstance(media_types, list)
            else media_types,
            "max_bytes": max_bytes,
            "slots": [_slot(item) for item in raw_slots]
            if isinstance(raw_slots, list)
            else raw_slots,
        }
    else:
        raise ValueError("artifact input contract is invalid")

    settings = document.get("settings")
    knobs = settings.get("knobs") if isinstance(settings, Mapping) else None
    raw_parameters: list[dict[str, object]] = []
    if knobs is not None:
        if not isinstance(knobs, Mapping):
            raise ValueError("artifact parameter contract is invalid")
        for name, setting in knobs.items():
            if not isinstance(setting, Mapping):
                raise TypeError("artifact parameter contract is invalid")
            value = setting.get("value")
            if isinstance(value, bool):
                kind = "boolean"
            elif isinstance(value, int):
                kind = "integer"
            elif isinstance(value, float):
                kind = "float"
            elif isinstance(value, str):
                kind = "string"
            else:
                raise TypeError("artifact parameter contract is invalid")
            raw_parameters.append(
                {
                    "name": name,
                    "type": kind,
                    "default": value,
                    "minimum": None,
                    "maximum": None,
                    "allowed_values": [],
                    "pattern": None,
                }
            )

    raw_output = interface.get("output")
    if not isinstance(raw_output, Mapping):
        raise TypeError("artifact output contract is unavailable")
    aggregate = raw_output.get("max_total_bytes")
    raw_slots = raw_output.get("slots")
    output_document = {
        "path": raw_output.get("path"),
        "max_total_bytes": aggregate,
        "slots": [_slot(item) for item in raw_slots]
        if isinstance(raw_slots, list)
        else raw_slots,
    }
    slots = output_document["slots"]
    output_media = (
        sorted({media for slot in slots for media in slot.get("media_types", [])})
        if isinstance(slots, list) and all(isinstance(slot, Mapping) for slot in slots)
        else []
    )
    output_limits = {
        "max_files": min(
            MAX_OUTPUT_FILES, sum(slot.get("max_files", 0) for slot in slots)
        )
        if isinstance(slots, list)
        and all(
            isinstance(slot, Mapping) and isinstance(slot.get("max_files"), int)
            for slot in slots
        )
        else 0,
        "max_file_bytes": max(
            (slot.get("max_file_bytes", 0) for slot in slots), default=0
        )
        if isinstance(slots, list)
        else 0,
        "max_total_bytes": aggregate,
        "allowed_media_types": output_media,
    }
    raw_document = {
        "schema_version": 1,
        "interface": interface_name,
        "input": input_document,
        "parameters": sorted(raw_parameters, key=lambda item: str(item.get("name"))),
        "output": output_document,
        "output_limits": output_limits,
        "max_timeout_seconds": 3_600,
    }
    if "engine" in interface:
        raw_document["engine"] = interface["engine"]
    try:
        return CompiledArtifactContract.model_validate(raw_document)
    except ValidationError as error:
        first = error.errors()[0]
        raise ValueError(
            str(first.get("msg", "artifact contract is invalid"))
        ) from error


__all__ = [
    "ArtifactInputContract",
    "ArtifactOutputContract",
    "ArtifactOutputLimits",
    "ArtifactSlotContract",
    "BooleanParameter",
    "CompiledArtifactContract",
    "EnumParameter",
    "FloatParameter",
    "IntegerParameter",
    "ParameterDefinition",
    "StringParameter",
    "compile_artifact_contract",
    "validate_parameter_definition",
]
