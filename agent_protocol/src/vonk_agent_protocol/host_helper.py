"""Canonical authorization protocol for the narrow root host helper."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError, canonical_message
from .package_upgrade import PackageRollbackAuthority
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
    RUNTIME_PREFLIGHT = "runtime-preflight"
    IMAGE_IMPORT = "image-import"
    IMAGE_INSPECT = "image-inspect"
    RUN_INSPECT = "run-inspect"
    START = "start"
    STOP = "stop"


class HostOperationKind(StrEnum):
    CREATE_MANAGED_DIRECTORY = "create-managed-directory"
    INSTALL_VONK_DEB = "install-vonk-deb"
    CONFIRM_PACKAGE_ACTIVATION = "confirm-package-activation"
    RESTART_VONK_UNIT = "restart-vonk-unit"
    SCHEDULE_REBOOT = "schedule-reboot"
    EXECUTE_CONTAINER_RUNTIME_REQUEST = "execute-container-runtime-request"


class _HostOperation(WireModel):
    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class CreateManagedDirectoryOperation(_HostOperation):
    type: Literal["create-managed-directory"]
    area: Literal["models", "state", "workloads"]
    relative_path: str = Field(min_length=1, max_length=512, strict=True)

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 512 or not _relative_path(value):
            raise ValueError("managed relative path is invalid")
        return value




class InstallVonkDebOperation(_HostOperation):
    type: Literal["install-vonk-deb"]
    package_sha256: Digest
    package_signature: Signature
    rollback: PackageRollbackAuthority


class ConfirmPackageActivationOperation(_HostOperation):
    type: Literal["confirm-package-activation"]
    package_sha256: Digest
    attempt_nonce: Digest


class RestartVonkUnitOperation(_HostOperation):
    type: Literal["restart-vonk-unit"]
    unit: Literal["agent", "helper"]


class ScheduleRebootOperation(_HostOperation):
    type: Literal["schedule-reboot"]
    delay_seconds: int = Field(ge=60, le=3600, strict=True)


class ExecuteContainerRuntimeRequestOperation(_HostOperation):
    type: Literal["execute-container-runtime-request"]
    action: Literal["runtime-preflight", "image-import", "image-inspect", "run-inspect", "start", "stop"]
    job_id: Uuid4Text
    operation_id: Uuid4Text
    attempt: int = Field(ge=1, le=2**31 - 1, strict=True)
    fence: Uuid4Text
    request_sha256: Digest
    observation_identity_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def observation_only_for_inspection(
        self,
    ) -> ExecuteContainerRuntimeRequestOperation:
        if (
            self.observation_identity_sha256 is not None
            and self.action != "run-inspect"
        ):
            raise ValueError("container runtime observation identity is invalid")
        return self


type HostOperation = Annotated[
    CreateManagedDirectoryOperation
    | InstallVonkDebOperation
    | ConfirmPackageActivationOperation
    | RestartVonkUnitOperation
    | ScheduleRebootOperation
    | ExecuteContainerRuntimeRequestOperation,
    Field(discriminator="type"),
]

_HOST_OPERATION_ADAPTER = TypeAdapter(HostOperation)


def parse_host_operation(value: Any) -> HostOperation:
    try:
        return _HOST_OPERATION_ADAPTER.validate_json(canonical_message(value))
    except (TypeError, ValueError, RecursionError, ValidationError) as error:
        raise AgentProtocolError("host helper operation is invalid") from error


class HostHelperGrantClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal["vonk.host-maintenance-helper"]
    request_id: Uuid4Text
    node_id: NodeId
    issued_at: int = Field(gt=0, strict=True)
    expires_at: int = Field(strict=True)
    operation: HostOperation

    @model_validator(mode="after")
    def bounded_expiry(self) -> HostHelperGrantClaims:
        if not 1 <= self.expires_at - self.issued_at <= MAX_HOST_HELPER_GRANT_SECONDS:
            raise ValueError("host helper grant expiry is invalid")
        return self

    @classmethod
    def parse(cls, value: Any) -> HostHelperGrantClaims:
        return _parse_model(cls, value, "host helper grant claims")

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class HostHelperSignature(WireModel):
    algorithm: Literal["ed25519"]
    key_id: Digest
    value: Signature

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
        return self.model_dump(mode="json")


class RecipeRunObservationReceiptClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal["vonk.recipe-run-observation-helper"]
    node_id: NodeId
    request_id: Uuid4Text
    request_sha256: Digest
    observation_identity_sha256: Digest
    outcome: Literal["running", "not-running"]
    observed_at: int = Field(gt=0, strict=True)

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationReceiptClaims:
        return _parse_model(cls, value, "recipe run observation receipt claims")

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class SignedRecipeRunObservationReceipt(WireModel):
    schema_version: Literal[1]
    claims: RecipeRunObservationReceiptClaims
    signature: HostHelperSignature

    @classmethod
    def parse(cls, value: Any) -> SignedRecipeRunObservationReceipt:
        return _parse_model(cls, value, "signed recipe run observation receipt")

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


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


def _relative_path(value: object) -> bool:
    return isinstance(value, str) and all(
        component not in {"", ".", ".."} and _COMPONENT.fullmatch(component) is not None
        for component in value.split("/")
    )


def _parse_model(cls: type[WireModel], value: Any, name: str) -> Any:
    try:
        return cls.model_validate_json(canonical_message(value))
    except (TypeError, ValueError, RecursionError, ValidationError) as error:
        raise AgentProtocolError(f"{name} is invalid") from error
