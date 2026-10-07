"""Canonical authorization protocol for the narrow root host helper."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .contracts import (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES,
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    AgentProtocolError,
    canonical_message,
)
from .package_upgrade import PackageRollbackAuthority
from .wire_model import MAX_RUN_GENERATION, WireModel

HOST_HELPER_AUTHORITY = "vonk.host-maintenance-helper"
HOST_HELPER_GRANT_DOMAIN = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
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
    "image-pull",
    "image-inspect",
    "run-inspect",
    "start",
    "stop",
    "installation-cleanup",
]


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
        le=MAX_RUN_GENERATION,
        strict=True,
        exclude_if=lambda value: value is None,
    )
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
        return self


class RestartUnit(StrEnum):
    AGENT = "agent"
    HELPER = "helper"


class ContainerRuntimeAction(StrEnum):
    RUNTIME_PREFLIGHT = "runtime-preflight"
    IMAGE_PULL = "image-pull"
    IMAGE_INSPECT = "image-inspect"
    RUN_INSPECT = "run-inspect"
    START = "start"
    STOP = "stop"
    INSTALLATION_CLEANUP = "installation-cleanup"


class HostOperationKind(StrEnum):
    INSTALL_VONK_DEB = "install-vonk-deb"
    CONFIRM_PACKAGE_ACTIVATION = "confirm-package-activation"
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
        le=MAX_RUN_GENERATION,
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
    installation_id: Uuid4Text | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reconciliation_identity: RecipeReconciliationIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def bind_action_authority(
        self,
    ) -> ExecuteContainerRuntimeRequestOperation:
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


class RecipeRunInspectionRequest(WireModel):
    """Helper frame for one read-only inspection of a managed run.

    Unlike every other helper operation it needs no Controller grant: it only
    reports whether the exact container of an agent-written ``run-inspect``
    runtime request is running.  With ``include_logs`` it also returns the
    bounded tail of that exact container's own output (``process_logs``), read
    without stopping it, so a workload that is running but not ready leaves
    evidence of what it was doing.
    """

    request_id: Uuid4Text
    request_sha256: Digest
    include_logs: bool | None = None


def host_helper_grant_signing_bytes(claims: HostHelperGrantClaims) -> bytes:
    if type(claims) is not HostHelperGrantClaims:
        raise AgentProtocolError("host helper grant claims are invalid")
    return HOST_HELPER_GRANT_DOMAIN + canonical_message(claims.to_mapping())


def _parse_model(cls: type[WireModel], value: Any, name: str) -> Any:
    try:
        return cls.model_validate_json(canonical_message(value))
    except (TypeError, ValueError, RecursionError, ValidationError) as error:
        raise AgentProtocolError(f"{name} is invalid") from error
