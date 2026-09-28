"""Canonical authorization protocol for the narrow root host helper."""

from __future__ import annotations

import hashlib
import ipaddress
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

from .contracts import (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES,
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    AgentProtocolError,
    canonical_message,
)
from .package_upgrade import PackageRollbackAuthority
from .wire_model import WireModel

HOST_HELPER_AUTHORITY = "vonk.host-maintenance-helper"
HOST_HELPER_GRANT_DOMAIN = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
HOST_ARTIFACT_DOMAIN = b"VONK-HOST-ARTIFACT-V1\x00"
RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY = "vonk.recipe-run-observation-helper"
RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN = b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\x00"
MAX_HOST_HELPER_GRANT_SECONDS = 300
# A complete runtime request carries its typed plan plus one frame's worth of
# projected argv and envelope. The signed helper frame itself carries only the
# grant and request digest, never the request document.
MAX_HELPER_FRAME_BYTES = 1024 * 1024
MAX_HOST_RUNTIME_REQUEST_BYTES = (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES + MAX_HELPER_FRAME_BYTES
)
HOST_RUNTIME_REQUEST_ENVELOPE_BYTES = 16 * 1024
MAX_HOST_RUNTIME_ARGUMENT_BYTES = (
    MAX_HELPER_FRAME_BYTES - HOST_RUNTIME_REQUEST_ENVELOPE_BYTES
)
MAX_ARGV_BYTES = MAX_HOST_RUNTIME_ARGUMENT_BYTES

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
NodeId = Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
Uuid4Text = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
ContainerRuntimeActionName = Literal[
    "runtime-preflight",
    "image-import",
    "image-inspect",
    "run-inspect",
    "start",
    "stop",
    "installation-cleanup",
]


class RecipeRunInspectionBinding(WireModel):
    """Exact run identity bound into a signed helper inspection request."""

    run_id: Uuid4Text
    installation_id: Uuid4Text
    recipe_revision_id: Uuid4Text
    recipe_content_sha256: Digest
    mapping_id: Uuid4Text
    mapping_generation: int = Field(ge=1, le=2**63 - 1, strict=True)
    run_generation: int = Field(ge=1, le=2**31 - 1, strict=True)
    image_digest: Digest
    artifact_set_digest: Digest
    model_identity: str = Field(min_length=3, max_length=1024)
    rank: int = Field(ge=0, le=1023, strict=True)
    role: str = Field(min_length=1, max_length=64)
    world_size: int = Field(ge=1, le=1024, strict=True)
    local_address: str | None = Field(
        min_length=2, max_length=45, json_schema_extra={"format": "ip"}
    )
    master_address: str | None = Field(
        min_length=2, max_length=45, json_schema_extra={"format": "ip"}
    )
    master_port: int | None = Field(ge=1024, le=65535, strict=True)
    port: int = Field(ge=1024, le=65535, strict=True)
    runtime_arguments_sha256: Digest

    @field_validator("local_address", "master_address")
    @classmethod
    def canonical_fabric_address(cls, value: str | None) -> str | None:
        if value is not None:
            address = ipaddress.ip_address(value)
            if (
                str(address) != value
                or address.is_loopback
                or address.is_unspecified
                or address.is_multicast
                or address.is_link_local
            ):
                raise ValueError("inspection address must be canonical and routable")
        return value

    @model_validator(mode="after")
    def exact_rendezvous(self) -> RecipeRunInspectionBinding:
        if self.rank >= self.world_size:
            raise ValueError("inspection rank is outside its world")
        rendezvous = (self.local_address, self.master_address, self.master_port)
        if (
            self.world_size == 1 and any(value is not None for value in rendezvous)
        ) or (self.world_size > 1 and any(value is None for value in rendezvous)):
            raise ValueError("inspection rendezvous is invalid")
        return self


class RecipeReconciliationIdentity(WireModel):
    """The install a reconciliation removes, carried through the cleanup grant."""

    installation_id: Uuid4Text
    plan_digest: Digest


class HostRuntimeRequest(WireModel):
    """The complete bytes hashed by the agent and admitted by the root helper."""

    action: ContainerRuntimeActionName
    fence: Uuid4Text
    # One request carries the whole container command line. The authoritative
    # size limit is the canonical request byte ceiling
    # (`MAX_HOST_RUNTIME_REQUEST_BYTES`), so there is deliberately no item count
    # here: every element costs at least three canonical bytes, so a count
    # ceiling cannot fire before the byte ceiling and can only refuse a
    # legitimate many-mount command line that fits. NUL is the one byte an exec
    # argv cannot carry; CR and LF are legal bytes, and an empty element is
    # legal too (the plan's opaque-argv contract admits it), so only the payload
    # ceiling bounds an item.
    arguments: list[Annotated[str, Field(pattern=r"^[^\x00]*$")]]
    start_plan: "RecipeStartPayload | None" = Field(  # noqa: F821, UP037
        default=None, exclude_if=lambda value: value is None
    )
    job_plan: "RecipeJobRunRequest | None" = Field(  # noqa: F821, UP037
        default=None, exclude_if=lambda value: value is None
    )
    stop_plan: "RecipeStopPayload | None" = Field(  # noqa: F821, UP037
        default=None, exclude_if=lambda value: value is None
    )
    run_generation: int | None = Field(
        default=None,
        ge=1,
        le=2**31 - 1,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    observation: RecipeRunInspectionBinding | None = None
    installation_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reconciliation_identity: RecipeReconciliationIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def bind_runtime_inspection(self) -> HostRuntimeRequest:
        argument_free = self.action in {
            "runtime-preflight",
            "stop",
            "installation-cleanup",
        }
        if (not self.arguments) != argument_free:
            raise ValueError("runtime arguments do not match the action")
        if (self.installation_id is not None) != (
            self.action == "installation-cleanup"
        ):
            raise ValueError("runtime installation identity does not match the action")
        if self.reconciliation_identity is not None and (
            self.action != "installation-cleanup"
            or self.installation_id != self.reconciliation_identity.installation_id
        ):
            raise ValueError(
                "runtime reconciliation identity does not match the action"
            )
        if self.action == "start":
            if (
                (self.start_plan is None) == (self.job_plan is None)
                or self.stop_plan is not None
                or self.run_generation is None
            ):
                raise ValueError("runtime start plan binding is invalid")
            plan = self.start_plan if self.start_plan is not None else self.job_plan
            assert plan is not None
            if self.run_generation != plan.run_generation:
                raise ValueError("runtime start generation does not match its plan")
            if len(canonical_message(plan.compiled_execution_plan)) > (
                MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
            ):
                raise ValueError("runtime start compiled plan exceeds its byte ceiling")
            if len(canonical_message(plan)) > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES:
                raise ValueError(
                    "runtime start plan exceeds its canonical byte ceiling"
                )
        elif self.action == "stop":
            plan = self.stop_plan
            if (
                plan is None
                or self.start_plan is not None
                or self.job_plan is not None
                or self.run_generation != plan.run_generation
            ):
                raise ValueError("runtime stop plan binding is invalid")
            if len(canonical_message(plan.compiled_execution_plan)) > (
                MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
            ):
                raise ValueError("runtime stop compiled plan exceeds its byte ceiling")
            if len(canonical_message(plan)) > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES:
                raise ValueError("runtime stop plan exceeds its canonical byte ceiling")
        elif any(
            value is not None
            for value in (
                self.start_plan,
                self.job_plan,
                self.stop_plan,
                self.run_generation,
            )
        ):
            raise ValueError("runtime plan binding does not match the action")
        if len(canonical_message(self)) > MAX_HOST_RUNTIME_REQUEST_BYTES:
            raise ValueError("runtime request exceeds its canonical byte ceiling")
        if self.observation is not None and (
            self.action != "run-inspect"
            or hashlib.sha256(canonical_message(self.arguments)).hexdigest()
            != self.observation.runtime_arguments_sha256
        ):
            raise ValueError("runtime observation binding does not match the request")
        return self


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
    INSTALLATION_CLEANUP = "installation-cleanup"


class HostOperationKind(StrEnum):
    INSTALL_VONK_DEB = "install-vonk-deb"
    CONFIRM_PACKAGE_ACTIVATION = "confirm-package-activation"
    RESTART_VONK_UNIT = "restart-vonk-unit"
    SCHEDULE_REBOOT = "schedule-reboot"
    EXECUTE_CONTAINER_RUNTIME_REQUEST = "execute-container-runtime-request"


class _HostOperation(WireModel):
    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


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
    action: ContainerRuntimeActionName
    fence: Uuid4Text
    request_sha256: Digest
    start_plan_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    stop_plan_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    run_generation: int | None = Field(
        default=None,
        ge=1,
        le=2**31 - 1,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    runtime_run_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    runtime_target_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    runtime_installation_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    observation_identity_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    installation_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reconciliation_identity: RecipeReconciliationIdentity | None = Field(
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
        if (self.installation_id is not None) != (
            self.action == "installation-cleanup"
        ):
            raise ValueError("container runtime installation identity is invalid")
        if self.reconciliation_identity is not None and (
            self.action != "installation-cleanup"
            or self.installation_id != self.reconciliation_identity.installation_id
        ):
            raise ValueError("container runtime reconciliation identity is invalid")
        if self.action == "start":
            if (
                self.start_plan_sha256 is None
                or self.stop_plan_sha256 is not None
                or self.run_generation is None
                or self.runtime_run_id is None
                or self.runtime_target_id is None
                or self.runtime_installation_id is None
            ):
                raise ValueError("container runtime start authority is invalid")
        elif self.action == "stop":
            if (
                self.stop_plan_sha256 is None
                or self.start_plan_sha256 is not None
                or self.run_generation is None
                or self.runtime_run_id is None
                or self.runtime_target_id is None
                or self.runtime_installation_id is None
            ):
                raise ValueError("container runtime stop authority is invalid")
        elif any(
            value is not None
            for value in (
                self.start_plan_sha256,
                self.stop_plan_sha256,
                self.run_generation,
                self.runtime_run_id,
                self.runtime_target_id,
                self.runtime_installation_id,
            )
        ):
            raise ValueError("container runtime plan authority is invalid")
        return self


type HostOperation = Annotated[
    InstallVonkDebOperation
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
    issued_at: int = Field(
        gt=0, strict=True, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )
    expires_at: int = Field(
        strict=True, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )
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
    observed_at: int = Field(
        gt=0, strict=True, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )

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


def _parse_model(cls: type[WireModel], value: Any, name: str) -> Any:
    try:
        return cls.model_validate_json(canonical_message(value))
    except (TypeError, ValueError, RecursionError, ValidationError) as error:
        raise AgentProtocolError(f"{name} is invalid") from error
