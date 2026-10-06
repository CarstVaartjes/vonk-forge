"""Canonical compiled contract for artifact-producing recipe jobs.

The recipe document is the authoring contract.  This module is the one typed
projection used after compilation.  The recipe's interface declares no engine
options, so the contract carries none.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import canonical_message
from vonk_forge_contracts import RecipeDefinition
from vonk_forge_contracts.recipe import RecipeFileSlot, RecipeJobInterface

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
    # Required on the abstract base so each concrete parameter states its own
    # empty default. A base default that a subclass removes is a legal Pydantic
    # override but is not something a dataclass-based checker can accept, and
    # inlining a default here would change the concrete schemas.
    allowed_values: tuple[ParameterScalar, ...] = Field(max_length=128)
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
    allowed_values: tuple[ParameterScalar, ...] = Field(
        default_factory=tuple, max_length=128
    )

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
    allowed_values: tuple[ParameterScalar, ...] = Field(
        default_factory=tuple, max_length=128
    )


class FloatParameter(_ArtifactParameterBase):
    type: Literal["float"]
    default: float | int
    minimum: float | int | None = None
    maximum: float | int | None = None
    allowed_values: tuple[ParameterScalar, ...] = Field(
        default_factory=tuple, max_length=128
    )

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
    allowed_values: tuple[ParameterScalar, ...] = Field(
        default_factory=tuple, max_length=128
    )


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


def validate_parameter_definition(raw: object) -> ParameterDefinition:
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


def _slot(item: RecipeFileSlot) -> ArtifactSlotContract:
    """Copy one recipe slot into its canonical (sorted) contract form."""

    return ArtifactSlotContract(
        id=item.id,
        label=item.label,
        description=item.description,
        media_types=tuple(sorted(item.media_types)),
        extensions=tuple(sorted(item.extensions)),
        min_files=item.min_files,
        max_files=item.max_files,
        max_file_bytes=item.max_file_bytes,
        max_total_bytes=item.max_total_bytes,
    )


def _input_contract(interface: RecipeJobInterface) -> ArtifactInputContract:
    declared = interface.input
    if declared is None:
        return ArtifactInputContract(
            required=False, media_types=(), max_bytes=0, slots=()
        )
    if declared.slots is None:
        # A recipe that declares one aggregate input is one unnamed slot.
        slots = (
            ArtifactSlotContract(
                id="input",
                label="Input",
                description="Recipe input",
                media_types=tuple(sorted(declared.media_types)),
                extensions=(),
                min_files=1 if declared.required else 0,
                max_files=MAX_INPUT_FILES,
                max_file_bytes=min(declared.max_bytes, MAX_INPUT_FILE_BYTES),
                max_total_bytes=declared.max_bytes,
            ),
        )
    else:
        slots = tuple(_slot(item) for item in declared.slots)
    return ArtifactInputContract(
        required=declared.required,
        media_types=tuple(sorted(set(declared.media_types))),
        max_bytes=declared.max_bytes,
        slots=slots,
    )


def _parameter(name: str, value: ParameterScalar) -> ParameterDefinition:
    """One recipe knob, as the parameter an artifact job may override."""

    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "float"
    else:
        kind = "string"
    return validate_parameter_definition(
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


def compile_artifact_contract(
    recipe: RecipeDefinition, interface_name: str
) -> CompiledArtifactContract:
    interface = next(
        (
            item
            for item in recipe.interfaces
            if isinstance(item, RecipeJobInterface) and item.adapter == interface_name
        ),
        None,
    )
    if interface is None:
        raise TypeError("artifact job interface contract is unavailable")
    try:
        slots = tuple(_slot(item) for item in interface.output.slots)
        parameters = tuple(
            sorted(
                (
                    _parameter(name, setting.value)
                    for name, setting in recipe.settings.knobs.items()
                ),
                key=lambda item: item.name,
            )
        )
        return CompiledArtifactContract.model_validate(
            {
                "schema_version": 1,
                "interface": interface_name,
                "input": _input_contract(interface),
                "parameters": parameters,
                "output": ArtifactOutputContract(
                    path="/outputs",
                    max_total_bytes=interface.output.max_total_bytes,
                    slots=slots,
                ),
                "output_limits": ArtifactOutputLimits(
                    max_files=min(MAX_OUTPUT_FILES, sum(s.max_files for s in slots)),
                    max_file_bytes=max((s.max_file_bytes for s in slots), default=0),
                    max_total_bytes=interface.output.max_total_bytes,
                    allowed_media_types=tuple(
                        sorted({media for s in slots for media in s.media_types})
                    ),
                ),
                "max_timeout_seconds": 3_600,
            }
        )
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
    "ParameterScalar",
    "StringParameter",
    "compile_artifact_contract",
    "validate_parameter_definition",
]
