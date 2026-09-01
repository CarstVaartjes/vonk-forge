"""Canonical authorization protocol for the narrow root host helper."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from .contracts import AgentProtocolError, canonical_message

HOST_HELPER_AUTHORITY = "vonk.host-maintenance-helper"
HOST_HELPER_GRANT_DOMAIN = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
HOST_ARTIFACT_DOMAIN = b"VONK-HOST-ARTIFACT-V1\x00"
RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY = "vonk.recipe-run-observation-helper"
RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN = b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\x00"
MAX_HOST_HELPER_GRANT_SECONDS = 300

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


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


@dataclass(frozen=True)
class HostHelperOperation:
    kind: HostOperationKind
    values: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.kind) is not HostOperationKind or not isinstance(
            self.values, Mapping
        ):
            raise AgentProtocolError("host helper operation is invalid")
        parsed = self._parse_values(dict(self.values))
        object.__setattr__(self, "values", MappingProxyType(parsed))

    @classmethod
    def parse(cls, value: Any) -> HostHelperOperation:
        document = _mapping(value, "host helper operation")
        kind_value = document.get("type")
        try:
            kind = HostOperationKind(kind_value)
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("host helper operation is invalid") from error
        return cls(kind, {key: item for key, item in document.items() if key != "type"})

    def to_mapping(self) -> dict[str, object]:
        return {"type": self.kind.value, **self.values}

    def _parse_values(self, values: dict[str, object]) -> dict[str, object]:
        if self.kind is HostOperationKind.CREATE_MANAGED_DIRECTORY:
            _exact(values, {"area", "relative_path"}, "managed directory operation")
            try:
                area = ManagedArea(values["area"])
            except (TypeError, ValueError) as error:
                raise AgentProtocolError("managed area is invalid") from error
            relative = values["relative_path"]
            if not _relative_path(relative):
                raise AgentProtocolError("managed relative path is invalid")
            return {"area": area.value, "relative_path": relative}
        if self.kind is HostOperationKind.INSTALL_VONK_DEB:
            _exact(
                values,
                {"package_sha256", "package_signature"},
                "package installation operation",
            )
            _digest(values["package_sha256"], "package")
            _signature(values["package_signature"], "package")
            return values
        if self.kind is HostOperationKind.RESTART_VONK_UNIT:
            _exact(values, {"unit"}, "unit restart operation")
            try:
                unit = RestartUnit(values["unit"])
            except (TypeError, ValueError) as error:
                raise AgentProtocolError("Vonk unit is invalid") from error
            return {"unit": unit.value}
        if self.kind is HostOperationKind.SCHEDULE_REBOOT:
            _exact(values, {"delay_seconds"}, "reboot operation")
            delay = values["delay_seconds"]
            if (
                not isinstance(delay, int)
                or isinstance(delay, bool)
                or not 60 <= delay <= 3600
            ):
                raise AgentProtocolError("reboot delay is invalid")
            return values
        if self.kind is HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST:
            base_fields = {
                "action",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "request_sha256",
            }
            fields = set(values)
            if fields not in (
                base_fields,
                base_fields | {"observation_identity_sha256"},
            ):
                raise AgentProtocolError("container runtime operation is invalid")
            try:
                action = ContainerRuntimeAction(values["action"])
            except (TypeError, ValueError) as error:
                raise AgentProtocolError(
                    "container runtime action is invalid"
                ) from error
            job_id = _random_uuid(values["job_id"], "container runtime job")
            operation_id = _random_uuid(
                values["operation_id"], "container runtime operation"
            )
            fence = _random_uuid(values["fence"], "container runtime fence")
            attempt = values["attempt"]
            if (
                not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or not 1 <= attempt <= 2**31 - 1
            ):
                raise AgentProtocolError("container runtime attempt is invalid")
            _digest(values["request_sha256"], "container runtime request")
            parsed = {
                "action": action.value,
                "job_id": job_id,
                "operation_id": operation_id,
                "attempt": attempt,
                "fence": fence,
                "request_sha256": values["request_sha256"],
            }
            observation_identity = values.get("observation_identity_sha256")
            if observation_identity is not None:
                if action is not ContainerRuntimeAction.RUN_INSPECT:
                    raise AgentProtocolError(
                        "container runtime observation identity is invalid"
                    )
                _digest(observation_identity, "container runtime observation identity")
                parsed["observation_identity_sha256"] = observation_identity
            return parsed
        raise AgentProtocolError("host helper operation is invalid")


@dataclass(frozen=True)
class HostHelperGrantClaims:
    schema_version: int
    authority: str
    request_id: str
    node_id: str
    issued_at: int
    expires_at: int
    operation: HostHelperOperation

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise AgentProtocolError("host helper grant version is invalid")
        if self.authority != HOST_HELPER_AUTHORITY:
            raise AgentProtocolError("host helper grant authority is invalid")
        try:
            request_id = UUID(self.request_id)
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("host helper request ID is invalid") from error
        if str(request_id) != self.request_id or request_id.version != 4:
            raise AgentProtocolError("host helper request ID is invalid")
        if (
            not isinstance(self.node_id, str)
            or _NODE_ID.fullmatch(self.node_id) is None
        ):
            raise AgentProtocolError("host helper node ID is invalid")
        if (
            not isinstance(self.issued_at, int)
            or isinstance(self.issued_at, bool)
            or self.issued_at <= 0
            or not isinstance(self.expires_at, int)
            or isinstance(self.expires_at, bool)
            or not 1
            <= self.expires_at - self.issued_at
            <= MAX_HOST_HELPER_GRANT_SECONDS
        ):
            raise AgentProtocolError("host helper grant expiry is invalid")
        if type(self.operation) is not HostHelperOperation:
            raise AgentProtocolError("host helper operation is invalid")

    @classmethod
    def parse(cls, value: Any) -> HostHelperGrantClaims:
        document = _mapping(value, "host helper grant claims")
        _exact(
            document,
            {
                "schema_version",
                "authority",
                "request_id",
                "node_id",
                "issued_at",
                "expires_at",
                "operation",
            },
            "host helper grant claims",
        )
        return cls(
            schema_version=document["schema_version"],
            authority=document["authority"],
            request_id=document["request_id"],
            node_id=document["node_id"],
            issued_at=document["issued_at"],
            expires_at=document["expires_at"],
            operation=HostHelperOperation.parse(document["operation"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "authority": self.authority,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "operation": self.operation.to_mapping(),
        }


@dataclass(frozen=True)
class HostHelperSignature:
    algorithm: str
    key_id: str
    value: str

    def __post_init__(self) -> None:
        if (
            self.algorithm != "ed25519"
            or not isinstance(self.key_id, str)
            or _DIGEST.fullmatch(self.key_id) is None
            or not isinstance(self.value, str)
            or _SIGNATURE.fullmatch(self.value) is None
        ):
            raise AgentProtocolError("host helper signature is invalid")

    @classmethod
    def parse(cls, value: Any) -> HostHelperSignature:
        document = _mapping(value, "host helper signature")
        _exact(document, {"algorithm", "key_id", "value"}, "host helper signature")
        return cls(document["algorithm"], document["key_id"], document["value"])

    def to_mapping(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "value": self.value,
        }


@dataclass(frozen=True)
class SignedHostHelperGrant:
    schema_version: int
    claims: HostHelperGrantClaims
    signature: HostHelperSignature

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise AgentProtocolError("signed host helper grant version is invalid")
        if (
            type(self.claims) is not HostHelperGrantClaims
            or type(self.signature) is not HostHelperSignature
        ):
            raise AgentProtocolError("signed host helper grant is invalid")

    @classmethod
    def parse(cls, value: Any) -> SignedHostHelperGrant:
        document = _mapping(value, "signed host helper grant")
        _exact(
            document,
            {"schema_version", "claims", "signature"},
            "signed host helper grant",
        )
        return cls(
            document["schema_version"],
            HostHelperGrantClaims.parse(document["claims"]),
            HostHelperSignature.parse(document["signature"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "claims": self.claims.to_mapping(),
            "signature": self.signature.to_mapping(),
        }


@dataclass(frozen=True)
class RecipeRunObservationReceiptClaims:
    schema_version: int
    authority: str
    node_id: str
    request_id: str
    request_sha256: str
    observation_identity_sha256: str
    outcome: str
    observed_at: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or isinstance(self.schema_version, bool)
            or self.authority != RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY
            or not isinstance(self.node_id, str)
            or _NODE_ID.fullmatch(self.node_id) is None
            or self.outcome not in {"running", "not-running"}
            or not isinstance(self.observed_at, int)
            or isinstance(self.observed_at, bool)
            or self.observed_at <= 0
        ):
            raise AgentProtocolError("recipe run observation receipt is invalid")
        _random_uuid(self.request_id, "recipe run observation receipt request")
        _digest(self.request_sha256, "recipe run observation receipt request")
        _digest(
            self.observation_identity_sha256,
            "recipe run observation receipt identity",
        )

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationReceiptClaims:
        document = _mapping(value, "recipe run observation receipt claims")
        _exact(
            document,
            {
                "schema_version",
                "authority",
                "node_id",
                "request_id",
                "request_sha256",
                "observation_identity_sha256",
                "outcome",
                "observed_at",
            },
            "recipe run observation receipt claims",
        )
        return cls(**document)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "authority": self.authority,
            "node_id": self.node_id,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "observation_identity_sha256": self.observation_identity_sha256,
            "outcome": self.outcome,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class SignedRecipeRunObservationReceipt:
    schema_version: int
    claims: RecipeRunObservationReceiptClaims
    signature: HostHelperSignature

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or isinstance(self.schema_version, bool)
            or type(self.claims) is not RecipeRunObservationReceiptClaims
            or type(self.signature) is not HostHelperSignature
        ):
            raise AgentProtocolError("signed recipe run observation receipt is invalid")

    @classmethod
    def parse(cls, value: Any) -> SignedRecipeRunObservationReceipt:
        document = _mapping(value, "signed recipe run observation receipt")
        _exact(
            document,
            {"schema_version", "claims", "signature"},
            "signed recipe run observation receipt",
        )
        return cls(
            document["schema_version"],
            RecipeRunObservationReceiptClaims.parse(document["claims"]),
            HostHelperSignature.parse(document["signature"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
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
    if kind not in {"agent", "deb"}:
        raise AgentProtocolError("host artifact kind is invalid")
    _digest(digest, "host artifact")
    return HOST_ARTIFACT_DOMAIN + kind.encode("ascii") + b"\x00" + bytes.fromhex(digest)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AgentProtocolError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise AgentProtocolError(f"{name} fields are invalid")


def _digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} digest is invalid")


def _signature(value: Any, name: str) -> None:
    if not isinstance(value, str) or _SIGNATURE.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} signature is invalid")


def _relative_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and all(
            component not in {"", ".", ".."}
            and _COMPONENT.fullmatch(component) is not None
            for component in value.split("/")
        )
    )


def _random_uuid(value: Any, name: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as error:
        raise AgentProtocolError(f"{name} ID is invalid") from error
    if not isinstance(value, str) or str(parsed) != value or parsed.version != 4:
        raise AgentProtocolError(f"{name} ID is invalid")
    return value
