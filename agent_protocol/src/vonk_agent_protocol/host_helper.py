"""Canonical authorization protocol for the narrow root host helper."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .contracts import AgentProtocolError, canonical_message
from .wire_model import WireModel

HOST_HELPER_AUTHORITY = "vonk.host-maintenance-helper"
HOST_HELPER_GRANT_DOMAIN = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
HOST_ARTIFACT_DOMAIN = b"VONK-HOST-ARTIFACT-V1\x00"
RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY = "vonk.recipe-run-observation-helper"
RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN = b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\x00"
MAX_HOST_HELPER_GRANT_SECONDS = 300

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
NodeId = Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
Uuid4Text = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


class ManagedArea(StrEnum):
    MODELS = "models"
    STATE = "state"
    WORKLOADS = "workloads"


class RestartUnit(StrEnum):
    AGENT = "agent"
    HELPER = "helper"


class ContainerRuntimeAction(StrEnum):
    IMAGE_IMPORT = "image-import"
    IMAGE_INSPECT = "image-inspect"
    RUN_INSPECT = "run-inspect"
    START = "start"
    STOP = "stop"


class HostOperationKind(StrEnum):
    CREATE_MANAGED_DIRECTORY = "create-managed-directory"
    INSTALL_VONK_DEB = "install-vonk-deb"
    RESTART_VONK_UNIT = "restart-vonk-unit"
    SCHEDULE_REBOOT = "schedule-reboot"
    EXECUTE_CONTAINER_RUNTIME_REQUEST = "execute-container-runtime-request"


class _ManagedDirectoryValues(WireModel):
    area: ManagedArea
    relative_path: str = Field(min_length=1, max_length=512, strict=True)

    @field_validator("area", mode="before")
    @classmethod
    def parse_area(cls, value: object) -> object:
        return _enum_value(ManagedArea, value, "managed area")

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 512 or not _relative_path(value):
            raise ValueError("managed relative path is invalid")
        return value


class _InstallDebValues(WireModel):
    package_sha256: Digest
    package_signature: Signature


class _RestartUnitValues(WireModel):
    unit: RestartUnit

    @field_validator("unit", mode="before")
    @classmethod
    def parse_unit(cls, value: object) -> object:
        return _enum_value(RestartUnit, value, "Vonk unit")


class _RebootValues(WireModel):
    delay_seconds: int = Field(ge=60, le=3600, strict=True)


class _RuntimeValues(WireModel):
    action: ContainerRuntimeAction
    job_id: Uuid4Text
    operation_id: Uuid4Text
    attempt: int = Field(ge=1, le=2**31 - 1, strict=True)
    fence: Uuid4Text
    request_sha256: Digest
    observation_identity_sha256: Digest | None = None

    @field_validator("action", mode="before")
    @classmethod
    def parse_action(cls, value: object) -> object:
        return _enum_value(ContainerRuntimeAction, value, "container runtime action")

    @model_validator(mode="after")
    def observation_only_for_inspection(self) -> _RuntimeValues:
        if (
            self.observation_identity_sha256 is not None
            and self.action is not ContainerRuntimeAction.RUN_INSPECT
        ):
            raise ValueError("container runtime observation identity is invalid")
        return self


_VALUES_MODELS = {
    HostOperationKind.CREATE_MANAGED_DIRECTORY: _ManagedDirectoryValues,
    HostOperationKind.INSTALL_VONK_DEB: _InstallDebValues,
    HostOperationKind.RESTART_VONK_UNIT: _RestartUnitValues,
    HostOperationKind.SCHEDULE_REBOOT: _RebootValues,
    HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST: _RuntimeValues,
}


class HostHelperOperation(WireModel):
    """One closed, flattened host operation."""

    kind: HostOperationKind
    values: dict[str, object]

    def __init__(
        self,
        kind: HostOperationKind | object | None = None,
        values: Mapping[str, object] | None = None,
        **data: object,
    ) -> None:
        if kind is not None or values is not None:
            if data:
                raise TypeError("host helper operation arguments are ambiguous")
            data = {"kind": kind, "values": values}
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def accept_flattened_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "type" in value and "kind" not in value:
            document = dict(value)
            try:
                kind = HostOperationKind(document.pop("type"))
            except (TypeError, ValueError) as error:
                raise ValueError("host helper operation is invalid") from error
            return {"kind": kind, "values": document}
        return value

    @model_validator(mode="after")
    def validate_values(self) -> HostHelperOperation:
        value_model = _VALUES_MODELS.get(self.kind)
        if value_model is None:
            raise ValueError("host helper operation is invalid")
        parsed = value_model.model_validate(self.values)
        object.__setattr__(
            self, "values", parsed.model_dump(mode="json", exclude_none=True)
        )
        return self

    @classmethod
    def parse(cls, value: Any) -> HostHelperOperation:
        document = _json_object(value, "host helper operation")
        try:
            kind = HostOperationKind(document.pop("type"))
            return cls(kind=kind, values=document)
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise AgentProtocolError("host helper operation is invalid") from error

    def to_mapping(self) -> dict[str, object]:
        return {"type": self.kind.value, **self.values}


class HostHelperGrantClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal["vonk.host-maintenance-helper"]
    request_id: Uuid4Text
    node_id: NodeId
    issued_at: int = Field(gt=0, strict=True)
    expires_at: int = Field(strict=True)
    operation: HostHelperOperation

    @field_validator("authority")
    @classmethod
    def exact_authority(cls, value: str) -> str:
        if value != HOST_HELPER_AUTHORITY:
            raise ValueError("host helper grant authority is invalid")
        return value

    @model_validator(mode="after")
    def bounded_expiry(self) -> HostHelperGrantClaims:
        if not 1 <= self.expires_at - self.issued_at <= MAX_HOST_HELPER_GRANT_SECONDS:
            raise ValueError("host helper grant expiry is invalid")
        return self

    @classmethod
    def parse(cls, value: Any) -> HostHelperGrantClaims:
        return _parse_model(cls, value, "host helper grant claims")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "operation": self.operation.to_mapping(),
        }


class HostHelperSignature(WireModel):
    algorithm: Literal["ed25519"]
    key_id: Digest
    value: Signature

    def __init__(
        self,
        algorithm: str | object | None = None,
        key_id: str | object | None = None,
        value: str | object | None = None,
        **data: object,
    ) -> None:
        if algorithm is not None or key_id is not None or value is not None:
            if data:
                raise TypeError("host helper signature arguments are ambiguous")
            data = {"algorithm": algorithm, "key_id": key_id, "value": value}
        super().__init__(**data)

    @field_validator("algorithm")
    @classmethod
    def exact_algorithm(cls, value: str) -> str:
        if value != "ed25519":
            raise ValueError("host helper signature is invalid")
        return value

    @classmethod
    def parse(cls, value: Any) -> HostHelperSignature:
        return _parse_model(cls, value, "host helper signature")

    def to_mapping(self) -> dict[str, str]:
        return self.model_dump(mode="json")


class SignedHostHelperGrant(WireModel):
    schema_version: Literal[1]
    claims: HostHelperGrantClaims
    signature: HostHelperSignature

    @classmethod
    def parse(cls, value: Any) -> SignedHostHelperGrant:
        return _parse_model(cls, value, "signed host helper grant")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "claims": self.claims.to_mapping(),
            "signature": self.signature.to_mapping(),
        }


class RecipeRunObservationReceiptClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal["vonk.recipe-run-observation-helper"]
    node_id: NodeId
    request_id: Uuid4Text
    request_sha256: Digest
    observation_identity_sha256: Digest
    outcome: Literal["running", "not-running"]
    observed_at: int = Field(gt=0, strict=True)

    @field_validator("authority")
    @classmethod
    def exact_authority(cls, value: str) -> str:
        if value != RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY:
            raise ValueError("recipe run observation receipt authority is invalid")
        return value

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationReceiptClaims:
        return _parse_model(cls, value, "recipe run observation receipt claims")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority,
            "node_id": self.node_id,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "observation_identity_sha256": self.observation_identity_sha256,
            "outcome": self.outcome,
            "observed_at": self.observed_at,
        }


class SignedRecipeRunObservationReceipt(WireModel):
    schema_version: Literal[1]
    claims: RecipeRunObservationReceiptClaims
    signature: HostHelperSignature

    @classmethod
    def parse(cls, value: Any) -> SignedRecipeRunObservationReceipt:
        return _parse_model(cls, value, "signed recipe run observation receipt")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "claims": self.claims.to_mapping(),
            "signature": self.signature.to_mapping(),
        }


def host_helper_grant_signing_bytes(claims: HostHelperGrantClaims) -> bytes:
    if type(claims) is not HostHelperGrantClaims:
        raise AgentProtocolError("host helper grant claims are invalid")
    return HOST_HELPER_GRANT_DOMAIN + canonical_message(claims.to_mapping())


def recipe_run_observation_receipt_signing_bytes(
    claims: RecipeRunObservationReceiptClaims,
) -> bytes:
    if type(claims) is not RecipeRunObservationReceiptClaims:
        raise AgentProtocolError("recipe run observation receipt claims are invalid")
    return RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN + canonical_message(
        claims.to_mapping()
    )


def host_artifact_signing_bytes(kind: str, digest: str) -> bytes:
    if (
        kind not in {"agent", "deb"}
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
    ):
        raise AgentProtocolError("host artifact is invalid")
    return HOST_ARTIFACT_DOMAIN + kind.encode("ascii") + b"\x00" + bytes.fromhex(digest)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _enum_value(enum_type: type[StrEnum], value: object, name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{name} is invalid")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error


def _relative_path(value: object) -> bool:
    return isinstance(value, str) and all(
        component not in {"", ".", ".."}
        and _COMPONENT.fullmatch(component) is not None
        for component in value.split("/")
    )


def _json_object(value: Any, name: str) -> dict[str, Any]:
    try:
        document = json.loads(canonical_message(value))
    except (TypeError, ValueError, RecursionError) as error:
        raise AgentProtocolError(f"{name} must be an object") from error
    if not isinstance(document, dict):
        raise AgentProtocolError(f"{name} must be an object")
    return document


def _parse_model(cls: type[WireModel], value: Any, name: str) -> Any:
    try:
        return cls.model_validate_json(canonical_message(value))
    except (TypeError, ValueError, RecursionError, ValidationError) as error:
        raise AgentProtocolError(f"{name} is invalid") from error
