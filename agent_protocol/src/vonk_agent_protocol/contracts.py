from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

MAX_DOCUMENT_BYTES = 64 * 1024
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
VERSIONED_PLATFORM_TARGET = re.compile(
    r"platform/releases/"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"[0-9a-f]{64}\.json\Z"
)
UNSAFE_KEY = re.compile(
    r"password|secret|token|authorization|private.?key|command|shell|environment",
    re.IGNORECASE,
)


def _ascii_case_pattern(token: str, *, initial_upper: bool = False) -> str:
    prefix = token[0].upper() if initial_upper else f"[{token[0].upper()}{token[0]}]"
    return prefix + "".join(
        f"[{character.upper()}{character}]" for character in token[1:]
    )


PATH_KEY_TOKENS = ("path", "file", "filename", "filepath", "directory", "folder")
PATH_KEY_ANY_CASE = "|".join(_ascii_case_pattern(token) for token in PATH_KEY_TOKENS)
PATH_KEY_CAMEL_CASE = "|".join(
    _ascii_case_pattern(token, initial_upper=True) for token in PATH_KEY_TOKENS
)
# A forbidden term starts at the key edge, after '_'/'-', or as an uppercase
# term after lowercase/digit. It ends at the key edge, before '_'/'-', or
# before an uppercase continuation. Matching inside each term is ASCII
# case-insensitive; a lowercase continuation such as "pathology" remains safe.
PATH_KEY = re.compile(
    rf"(?:^|[_-])(?:{PATH_KEY_ANY_CASE})(?:$|[_-]|[A-Z])"
    rf"|[a-z0-9](?:{PATH_KEY_CAMEL_CASE})(?:$|[_-]|[A-Z])"
)


class AgentProtocolError(ValueError):
    """A protocol message is invalid or outside the agent trust boundary."""


class AgentOperation(StrEnum):
    NODE_PROBE = "node.probe"
    RELEASE_INSTALL = "release.install"
    WORKLOAD_PREPARE = "workload.prepare"
    WORKLOAD_START = "workload.start"
    WORKLOAD_STOP = "workload.stop"
    WORKLOAD_HEALTH = "workload.health"
    WORKLOAD_VERIFY = "workload.verify"
    AGENT_UPDATE = "agent.update"
    AGENT_ROLLBACK = "agent.rollback"
    PACKAGE_PREPARE = "package.prepare"
    PACKAGE_ACTIVATE = "package.activate"
    PACKAGE_HEALTH = "package.health"
    PACKAGE_STOP = "package.stop"
    PACKAGE_ROLLBACK = "package.rollback"
    PACKAGE_REMOVE = "package.remove"
    PACKAGE_REPAIR = "package.repair"
    PACKAGE_GC = "package.gc"
    RECIPE_BUILD = "recipe.build.v1"
    RECIPE_IMAGE_IMPORT = "recipe.image.import.v1"
    RECIPE_INSTALL = "recipe.install"
    RECIPE_START = "recipe.start"
    RECIPE_STOP = "recipe.stop"
    RECIPE_UNINSTALL = "recipe.uninstall"


PROTOCOL_FORMAT_CHECKER = FormatChecker()


@PROTOCOL_FORMAT_CHECKER.checks("date-time")
def _is_utc_date_time(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return False
    return deadline.tzinfo is not None and deadline.utcoffset() == UTC.utcoffset(
        deadline
    )


def canonical_message(value: Any) -> bytes:
    """Encode a protocol value with deterministic UTF-8 JSON."""
    try:
        return json.dumps(
            _to_wire(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AgentProtocolError("message must contain JSON values") from error


def _to_wire(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_wire(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _to_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_wire(item) for item in value]
    return value


def _canonical_copy(value: Any, *, name: str) -> Any:
    try:
        copied = json.loads(canonical_message(value))
    except AgentProtocolError as error:
        raise AgentProtocolError(f"{name} must be JSON") from error
    return _freeze(copied)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_safe_keys(
    value: Any,
    *,
    field_name: str | None = None,
    allow_secret_refs: bool = False,
    secret_value: bool = False,
    operation: AgentOperation | None = None,
    path: tuple[str | int, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentProtocolError("JSON object keys must be strings")
            if _is_path_key(key):
                raise AgentProtocolError(f"filesystem path key is not allowed: {key}")
            if UNSAFE_KEY.search(key) and not (
                allow_secret_refs and (key == "secrets" or field_name == "secrets")
            ):
                raise AgentProtocolError(f"unsafe protocol key: {key}")
            _validate_safe_keys(
                item,
                field_name=key,
                allow_secret_refs=allow_secret_refs or key == "deployment",
                secret_value=field_name == "secrets",
                operation=operation,
                path=(*path, key),
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_safe_keys(
                item,
                allow_secret_refs=allow_secret_refs,
                secret_value=secret_value,
                operation=operation,
                path=(*path, index),
            )
    elif isinstance(value, str):
        if secret_value:
            if not value.startswith("secret://"):
                raise AgentProtocolError("secret reference is not canonical")
            return
        if field_name == "platform_target_name":
            if VERSIONED_PLATFORM_TARGET.fullmatch(value) is None:
                raise AgentProtocolError("platform target identifier is not canonical")
        elif operation is AgentOperation.RECIPE_BUILD and _typed_build_string(
            path, value
        ):
            return
        elif "/" in value or "\\" in value:
            raise AgentProtocolError("filesystem path values are not allowed")


def _is_path_key(key: str) -> bool:
    return bool(PATH_KEY.search(key))


def _typed_build_string(path: tuple[str | int, ...], value: str) -> bool:
    if path == ("platform",):
        return value == "linux/arm64"
    if path == ("dockerfile",):
        return (
            0 < len(value) <= 512
            and not value.startswith("/")
            and "\\" not in value
            and "\x00" not in value
            and all(part not in {"", ".", ".."} for part in value.split("/"))
        )
    return (
        len(path) == 3
        and path[0] == "arguments"
        and isinstance(path[1], int)
        and path[2] == "value"
        and len(value) <= 1024
        and "\x00" not in value
    )


def _validate_bounded_document(
    value: Any, *, name: str, operation: AgentOperation | None = None
) -> Any:
    if not isinstance(value, Mapping):
        raise AgentProtocolError(f"{name} must be a JSON object")
    _validate_safe_keys(value, operation=operation)
    copied = _canonical_copy(value, name=name)
    if len(canonical_message(copied)) > MAX_DOCUMENT_BYTES:
        raise AgentProtocolError(f"{name} is too large")
    return copied


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentProtocolError("message must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise AgentProtocolError("JSON object keys must be strings")
    return value


def _fields(value: Mapping[str, Any], *, required: set[str]) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        detail = (
            f"missing fields: {', '.join(missing)}"
            if missing
            else f"unknown fields: {', '.join(unknown)}"
        )
        raise AgentProtocolError(detail)


def _version(value: Any) -> int:
    if value != 1 or isinstance(value, bool):
        raise AgentProtocolError("unsupported schema_version")
    return 1


def _uuid(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise AgentProtocolError(f"{name} must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AgentProtocolError(f"{name} must be a UUID") from error
    if str(parsed) != value:
        raise AgentProtocolError(f"{name} must be a canonical UUID")
    return value


def _attempt(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentProtocolError("attempt must be a positive integer")
    return value


def _node_id(value: Any) -> str:
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise AgentProtocolError("node_id must match spk_[0-9a-f]{32}")
    return value


def _deadline(value: Any) -> datetime:
    if isinstance(value, datetime):
        deadline = value
    elif isinstance(value, str):
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError as error:
            raise AgentProtocolError(
                "deadline must be an ISO-8601 UTC timestamp"
            ) from error
    else:
        raise AgentProtocolError("deadline must be an ISO-8601 UTC timestamp")
    if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(deadline):
        raise AgentProtocolError("deadline must be aware UTC")
    return deadline.astimezone(UTC)


def _attempt_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _version(value["schema_version"]),
        "job_id": _uuid(value["job_id"], name="job_id"),
        "operation_id": _uuid(value["operation_id"], name="operation_id"),
        "attempt": _attempt(value["attempt"]),
        "fence": _uuid(value["fence"], name="fence"),
        "node_id": _node_id(value["node_id"]),
        "deadline": _deadline(value["deadline"]),
    }


@dataclass(frozen=True)
class AgentClaim:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    operation: AgentOperation
    base_commit: str
    payload_digest: str
    payload: Mapping[str, Any]
    deadline: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(
            self, "operation_id", _uuid(self.operation_id, name="operation_id")
        )
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        if not isinstance(self.operation, AgentOperation):
            raise AgentProtocolError("operation is not supported")
        if not isinstance(self.base_commit, str) or not COMMIT.fullmatch(
            self.base_commit
        ):
            raise AgentProtocolError(
                "base_commit must be a 40-character lowercase SHA-1"
            )
        if not isinstance(self.payload_digest, str) or not DIGEST.fullmatch(
            self.payload_digest
        ):
            raise AgentProtocolError("payload_digest must be a lowercase SHA-256")
        payload = _validate_bounded_document(
            self.payload, name="payload", operation=self.operation
        )
        if (
            hashlib.sha256(canonical_message(payload)).hexdigest()
            != self.payload_digest
        ):
            raise AgentProtocolError("payload digest does not match payload")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "deadline", _deadline(self.deadline))

    @classmethod
    def parse(cls, raw: Any) -> AgentClaim:
        value = _mapping(raw)
        _fields(
            value,
            required={
                "schema_version",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "node_id",
                "operation",
                "base_commit",
                "payload_digest",
                "payload",
                "deadline",
            },
        )
        try:
            operation = AgentOperation(value["operation"])
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("operation is not supported") from error
        base_commit = value["base_commit"]
        if not isinstance(base_commit, str) or not COMMIT.fullmatch(base_commit):
            raise AgentProtocolError(
                "base_commit must be a 40-character lowercase SHA-1"
            )
        payload_digest = value["payload_digest"]
        if not isinstance(payload_digest, str) or not DIGEST.fullmatch(payload_digest):
            raise AgentProtocolError("payload_digest must be a lowercase SHA-256")
        return cls(
            **_attempt_fields(value),
            operation=operation,
            base_commit=base_commit,
            payload_digest=payload_digest,
            payload=value["payload"],
        )


@dataclass(frozen=True)
class AgentProgress:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    progress: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(
            self, "operation_id", _uuid(self.operation_id, name="operation_id")
        )
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        object.__setattr__(
            self, "progress", _validate_bounded_document(self.progress, name="progress")
        )

    @classmethod
    def parse(cls, raw: Any) -> AgentProgress:
        value = _mapping(raw)
        _fields(
            value,
            required={
                "schema_version",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "node_id",
                "deadline",
                "progress",
            },
        )
        return cls(**_attempt_fields(value), progress=value["progress"])


@dataclass(frozen=True)
class AgentResult:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    state: str
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(
            self, "operation_id", _uuid(self.operation_id, name="operation_id")
        )
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        if self.state not in {"succeeded", "failed", "waiting-for-operator"}:
            raise AgentProtocolError("result state is not supported")
        object.__setattr__(
            self, "result", _validate_bounded_document(self.result, name="result")
        )

    @classmethod
    def parse(cls, raw: Any) -> AgentResult:
        value = _mapping(raw)
        _fields(
            value,
            required={
                "schema_version",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "node_id",
                "deadline",
                "state",
                "result",
            },
        )
        return cls(
            **_attempt_fields(value), state=value["state"], result=value["result"]
        )


def schema_validator(schema_name: str) -> Draft202012Validator:
    """Return the package-mandated Draft 2020-12 validator for a wire schema."""
    if schema_name not in {
        "agent-job.schema.json",
        "agent-result.schema.json",
        "agent-directive.schema.json",
    }:
        raise AgentProtocolError(f"unknown protocol schema: {schema_name}")
    try:
        document = json.loads(
            (
                importlib.resources.files("vonk_agent_protocol")
                / "schemas"
                / schema_name
            ).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise AgentProtocolError("packaged protocol schema is invalid") from error
    return Draft202012Validator(document, format_checker=PROTOCOL_FORMAT_CHECKER)


def validate_schema_message(schema_name: str, raw: Any) -> Any:
    """Apply the format-aware wire schema and its mandatory runtime limits."""
    from .package_operations import AgentDirective

    parsers = {
        "agent-job.schema.json": AgentClaim.parse,
        "agent-result.schema.json": AgentResult.parse,
        "agent-directive.schema.json": AgentDirective.parse,
    }
    try:
        parser = parsers[schema_name]
    except KeyError as error:
        raise AgentProtocolError(f"unknown protocol schema: {schema_name}") from error
    errors = list(schema_validator(schema_name).iter_errors(raw))
    if errors:
        raise AgentProtocolError(f"schema validation failed: {errors[0].message}")
    return parser(raw)
