from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    model_validator,
)

from .wire_model import OperationProgress, WireModel

MAX_DOCUMENT_BYTES = 64 * 1024
MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES = (
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES
)
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
AUTHORITY_REVISION = re.compile(r"[0-9a-f]{64}\Z")
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
MODEL_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})\Z")
MODEL_REPOSITORY = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,7}\Z"
)
MODEL_QUERY_COMPONENT = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
PINNED_OCI_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,511}@sha256:[0-9a-f]{64}\Z")


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
UNSAFE_SCHEMA_KEY_PATTERN = (
    r"[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|"
    r"[Ss][Ee][Cc][Rr][Ee][Tt]|[Tt][Oo][Kk][Ee][Nn]|"
    r"[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]|"
    r"[Pp][Rr][Ii][Vv][Aa][Tt][Ee].?[Kk][Ee][Yy]|"
    r"[Cc][Oo][Mm][Mm][Aa][Nn][Dd]|[Ss][Hh][Ee][Ll][Ll]|"
    r"[Ee][Nn][Vv][Ii][Rr][Oo][Nn][Mm][Ee][Nn][Tt]"
)
AGENT_PACKAGE_URL = re.compile(
    r"https://install\.vonkforge\.ai/"
    r"[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}/vonk-forge-agent\.deb\Z"
)


class AgentProtocolError(ValueError):
    """A protocol message is invalid or outside the agent trust boundary."""


_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CanonicalUUID = Annotated[
    str,
    Field(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    ),
]
NodeIdentifier = Annotated[
    str,
    Field(strict=True, pattern=r"^spk_[0-9a-f]{32}$"),
]
DigestText = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class AgentOperation(StrEnum):
    AGENT_UPGRADE = "agent.upgrade.v1"
    ARTIFACT_DISTRIBUTION = "artifact.distribution.v1"
    RECIPE_BUILD = "recipe.build.v1"
    RECIPE_IMAGE_IMPORT = "recipe.image.import.v1"
    RECIPE_INSTALL = "recipe.install"
    RECIPE_START = "recipe.start"
    RECIPE_JOB_RUN = "recipe.job.run.v1"
    RECIPE_STOP = "recipe.stop"
    RECIPE_UNINSTALL = "recipe.uninstall"
    RECIPE_MODEL_UNINSTALL = "recipe.model-uninstall.v1"


class ArtifactDistributionPayload(WireModel):
    """The complete payload accepted by the artifact transfer operation."""

    schema_version: Literal[1]
    authority_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def plan_is_authority(self) -> ArtifactDistributionPayload:
        if self.plan_digest != self.authority_revision:
            raise ValueError("artifact distribution plan identity is invalid")
        return self


class AgentUpgradePayload(WireModel):
    """Signed package authority for the current agent upgrade operation."""

    architecture: Literal["linux-arm64"]
    package_bytes: int = Field(strict=True, ge=1, le=1024**3)
    package_sha256: DigestText
    package_signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
    package_url: Annotated[
        str,
        Field(
            pattern=r"^https://install\.vonkforge\.ai/[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}/vonk-forge-agent\.deb$"
        ),
    ]
    package_version: Annotated[str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$")]
    schema_version: Literal[1]
    target_binary_digest: DigestText
    target_build_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class AgentInstallResult(WireModel):
    installed_bytes: int = Field(strict=True, ge=0, le=16 * 1024**4)


class AgentUpgradeResult(WireModel):
    """Evidence emitted after the Rust agent reports an exact upgrade."""

    architecture: Literal["linux-arm64"]
    binary_digest: DigestText
    build_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    package_sha256: DigestText
    package_version: Annotated[
        str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$")
    ]
    self_test_passed: Literal[True]
    status: Literal["upgraded"]


class _RecipeStartEvidenceCommon(WireModel):
    recipe_revision_id: CanonicalUUID
    recipe_content_sha256: DigestText
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    artifact_set_digest: DigestText
    model_identity: str | None = Field(default=None, max_length=1024)
    rank: int = Field(strict=True, ge=0)
    world_size: int = Field(strict=True, ge=1)
    memory_reservation_bytes: int = Field(strict=True, ge=1)
    evidence_digest: DigestText


class RecipeStartSingleEvidence(_RecipeStartEvidenceCommon):
    rank: Literal[0]
    world_size: Literal[1]
    endpoint: str
    ready: Literal[True]
    run_generation: int = Field(strict=True, ge=1)
    runtime_arguments_sha256: DigestText
    local_address: None
    master_address: None
    master_port: None


class RecipeStartRankLaunchEvidence(_RecipeStartEvidenceCommon):
    phase: Literal["rank-launch"]
    run_id: CanonicalUUID
    run_generation: int = Field(strict=True, ge=1)
    runtime_arguments_sha256: DigestText
    role: str = Field(min_length=1, max_length=80)
    local_address: str | None
    master_address: str | None
    master_port: int | None = Field(default=None, strict=True, ge=1024, le=65535)
    process_running: Literal[True]
    fabric_projection_bound: Literal[True]
    launched: Literal[True]


class RecipeStartCollectiveReadinessEvidence(_RecipeStartEvidenceCommon):
    phase: Literal["collective-readiness"]
    run_id: CanonicalUUID
    run_generation: int = Field(strict=True, ge=1)
    runtime_arguments_sha256: DigestText
    role: str = Field(min_length=1, max_length=80)
    local_address: str | None
    master_address: str | None
    master_port: int | None = Field(default=None, strict=True, ge=1024, le=65535)
    endpoint: str
    ready: Literal[True]


RecipeStartEvidence = (
    RecipeStartSingleEvidence
    | RecipeStartRankLaunchEvidence
    | RecipeStartCollectiveReadinessEvidence
)


class RecipeStartResult(WireModel):
    endpoint: str | None = None
    evidence: RecipeStartEvidence
    evidence_digest: DigestText


class ArtifactDistributionResult(WireModel):
    assignment_id: CanonicalUUID
    model_artifact_set_sha256: DigestText
    verified: Literal[True]
    verified_digests: list[DigestText] = Field(max_length=4096)
    verified_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    imported_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    verified_oci_layout_sha256: DigestText
    oci_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    downloaded_bytes: int = Field(strict=True, ge=0, le=16 * 1024**4)
    evidence_digest: DigestText


class AgentFailureResult(WireModel):
    reason: str | None = Field(default=None, min_length=1, max_length=1024)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    summary: str | None = Field(default=None, min_length=1, max_length=1024)
    uncertain: bool | None = None
    recovery: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["failed"] | None = None
    operation: AgentOperation | None = None
    stage: str | None = Field(default=None, min_length=1, max_length=128)
    diagnostic: str | None = Field(default=None, min_length=1, max_length=512)
    helper_error_code: str | None = Field(default=None, min_length=1, max_length=128)
    helper_exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)

    @model_validator(mode="after")
    def requires_failure_identity(self) -> AgentFailureResult:
        if self.reason is None and self.error_code is None:
            raise ValueError("failure result requires reason or error_code")
        return self


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
    if isinstance(value, BaseModel):
        return _to_wire(value.model_dump(mode="python", exclude_unset=True))
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
    typed_result_strings: bool = False,
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentProtocolError("JSON object keys must be strings")
            typed_recipe_build_key = (
                operation is AgentOperation.RECIPE_BUILD
                and (
                    (
                        path == ("options",)
                        and key in {"environment", "ignorefile", "unset_environment"}
                    )
                    or (
                        len(path) == 3
                        and path[0] == "options"
                        and path[1] == "additional_contexts"
                        and isinstance(path[2], int)
                        and key == "path"
                    )
                )
            )
            typed_distribution_object_name = (
                operation is AgentOperation.ARTIFACT_DISTRIBUTION
                and len(path) == 3
                and path[0] == "distribution_assignment"
                and path[1] == "objects"
                and isinstance(path[2], int)
                and key == "name"
            )
            typed_compiled_plan_key = (
                operation in {AgentOperation.RECIPE_INSTALL, AgentOperation.RECIPE_START}
                and path[:1] == ("compiled_execution_plan",)
            )
            if _is_path_key(key) and not (
                typed_recipe_build_key
                or typed_distribution_object_name
                or typed_compiled_plan_key
                or (
                    operation is AgentOperation.RECIPE_JOB_RUN
                    and path == ("output_limits",)
                    and key == "max_file_bytes"
                )
            ):
                raise AgentProtocolError(f"filesystem path key is not allowed: {key}")
            if UNSAFE_KEY.search(key) and not (
                typed_recipe_build_key
                or (
                    allow_secret_refs
                    and (key == "secrets" or field_name == "secrets")
                )
            ):
                raise AgentProtocolError(f"unsafe protocol key: {key}")
            _validate_safe_keys(
                item,
                field_name=key,
                allow_secret_refs=allow_secret_refs or key == "deployment",
                secret_value=field_name == "secrets",
                operation=operation,
                path=(*path, key),
                typed_result_strings=typed_result_strings,
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_safe_keys(
                item,
                allow_secret_refs=allow_secret_refs,
                secret_value=secret_value,
                operation=operation,
                path=(*path, index),
                typed_result_strings=typed_result_strings,
            )
    elif isinstance(value, str):
        if secret_value:
            if not value.startswith("secret://"):
                raise AgentProtocolError("secret reference is not canonical")
            return
        if field_name == "platform_target_name":
            if VERSIONED_PLATFORM_TARGET.fullmatch(value) is None:
                raise AgentProtocolError("platform target identifier is not canonical")
        elif (
            (
                operation is AgentOperation.RECIPE_BUILD
                and _typed_build_string(path, value)
            )
            or (
                operation is AgentOperation.RECIPE_JOB_RUN
                and _typed_recipe_job_string(path, value)
            )
            or (
                operation is AgentOperation.AGENT_UPGRADE
                and path == ("package_url",)
                and AGENT_PACKAGE_URL.fullmatch(value) is not None
            )
            or (
                operation is AgentOperation.ARTIFACT_DISTRIBUTION
                and len(path) == 4
                and path[0] == "distribution_assignment"
                and path[1] == "objects"
                and isinstance(path[2], int)
                and path[3] == "name"
            )
            or (
                typed_result_strings
                and ("/" in value or "\\" in value)
                and _typed_result_string(path, value)
            )
        ) or (
            operation in {AgentOperation.RECIPE_INSTALL, AgentOperation.RECIPE_START}
            and path[:1] == ("compiled_execution_plan",)
        ):
            return
        elif "/" in value or "\\" in value:
            raise AgentProtocolError("filesystem path values are not allowed")


def _is_path_key(key: str) -> bool:
    return bool(PATH_KEY.search(key))


def _typed_build_string(path: tuple[str | int, ...], value: str) -> bool:
    def bundle_path() -> bool:
        return (
            0 < len(value.encode("utf-8")) <= 512
            and not value.startswith("/")
            and "\\" not in value
            and "\x00" not in value
            and all(part not in {"", ".", ".."} for part in value.split("/"))
        )

    if path == ("platform",):
        return value == "linux/arm64"
    if path == ("dockerfile",):
        return bundle_path()
    if path == ("options", "ignorefile"):
        return bundle_path()
    if (
        len(path) == 4
        and path[:2] == ("options", "additional_contexts")
        and isinstance(path[2], int)
        and path[3] == "path"
    ):
        return bundle_path()
    if (
        len(path) == 4
        and path[0] == "options"
        and path[1] in {"annotations", "environment", "labels", "layer_labels"}
        and isinstance(path[2], int)
        and path[3] in {"name", "value"}
    ):
        return len(value) <= 1024 and "\x00" not in value
    if (
        len(path) == 3
        and path[:2] == ("options", "unset_labels")
        and isinstance(path[2], int)
    ):
        return len(value) <= 128 and "\x00" not in value
    if (
        len(path) == 3
        and path[0] == "base_images"
        and isinstance(path[1], int)
        and path[2] == "reference"
    ):
        return PINNED_OCI_IMAGE.fullmatch(value) is not None
    return (
        len(path) == 3
        and path[0] == "arguments"
        and isinstance(path[1], int)
        and path[2] == "value"
        and len(value) <= 1024
        and "\x00" not in value
    )


def _typed_recipe_job_string(path: tuple[str | int, ...], value: str) -> bool:
    if (
        (
            len(path) == 3
            and path[0] == "inputs"
            and isinstance(path[1], int)
            and path[2] == "media_type"
        )
        or (
            len(path) == 3
            and path[:2] == ("output_limits", "allowed_media_types")
            and isinstance(path[2], int)
        )
        or (
            len(path) == 3
            and path[0] == "output_mappings"
            and isinstance(path[1], int)
            and path[2] == "media_type"
        )
    ):
        return bool(
            re.fullmatch(
                r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}",
                value,
            )
        )
    if path and path[0] == "parameters":
        return len(value.encode("utf-8")) <= 4096 and "\x00" not in value
    return False


def _typed_result_string(path: tuple[str | int, ...], value: str) -> bool:
    if path in {("endpoint",), ("evidence", "endpoint")}:
        return _recipe_endpoint(value)
    if path == ("evidence", "model_identity"):
        return _model_identity(value)
    if (
        len(path) == 4
        and path[:2] == ("output_manifest", "files")
        and isinstance(path[2], int)
        and path[3] == "media_type"
    ):
        return bool(
            re.fullmatch(
                r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}",
                value,
            )
        )
    return False


def _recipe_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        address = ipaddress.ip_address(hostname) if hostname is not None else None
    except ValueError:
        return False
    return bool(
        parsed.scheme == "http"
        and hostname
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and address is not None
        and not address.is_loopback
        and not address.is_unspecified
        and not address.is_multicast
    )


def _model_identity(value: str) -> bool:
    repository, marker, revision = value.rpartition("@")
    if (
        marker != "@"
        or not 1 <= len(repository) <= 512
        or MODEL_REVISION.fullmatch(revision) is None
        or "\\" in repository
    ):
        return False
    if MODEL_REPOSITORY.fullmatch(repository) is not None:
        return True
    try:
        parsed = urlsplit(repository)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path not in {"", "/"}
        and _safe_model_query(parsed.query)
        and not parsed.fragment
    )


def parse_model_identity(value: str) -> tuple[str, str]:
    """Parse the canonical result identity ``repository@revision``."""

    if not isinstance(value, str) or not _model_identity(value):
        raise AgentProtocolError("model identity is invalid")
    repository, _marker, revision = value.rpartition("@")
    return repository, revision


def format_model_identity(
    publisher: str, slug: str, content_sha256: str
) -> str:
    """Format the catalog model identity used by result evidence."""

    value = f"{publisher}/{slug}@{content_sha256}"
    parse_model_identity(value)
    return value


def _safe_model_query(query: str) -> bool:
    if not query:
        return True
    if len(query) > 256:
        return False
    try:
        fields = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except ValueError:
        return False
    return bool(
        fields
        and all(
            len(key) <= 64
            and MODEL_QUERY_COMPONENT.fullmatch(key) is not None
            and (not value or MODEL_QUERY_COMPONENT.fullmatch(value) is not None)
            and UNSAFE_KEY.search(key) is None
            for key, value in fields
        )
    )


def _validate_bounded_document(
    value: Any,
    *,
    name: str,
    operation: AgentOperation | None = None,
    typed_result_strings: bool = False,
    maximum_bytes: int = MAX_DOCUMENT_BYTES,
) -> Any:
    if not isinstance(value, Mapping):
        raise AgentProtocolError(f"{name} must be a JSON object")
    _validate_safe_keys(
        value,
        operation=operation,
        typed_result_strings=typed_result_strings,
    )
    copied = _canonical_copy(value, name=name)
    if len(canonical_message(copied)) > maximum_bytes:
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


from .build_import import (
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeImageImportEvidence,
    RecipeImageImportRequest,
)
from .recipe_jobs import RecipeJobRunRequest, RecipeJobRunResult
from .recipe_operations import (
    RecipeInstallPayload,
    RecipeModelCleanupPayload,
    RecipeModelCleanupResult,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeStopResult,
    RecipeUninstallPayload,
    RecipeUninstallResult,
)

AgentPayload = (
    AgentUpgradePayload
    | ArtifactDistributionPayload
    | RecipeBuildRequest
    | RecipeImageImportRequest
    | RecipeJobRunRequest
    | RecipeInstallPayload
    | RecipeStartPayload
    | RecipeStopPayload
    | RecipeUninstallPayload
    | RecipeModelCleanupPayload
)
AgentResultPayload = (
    AgentInstallResult
    | RecipeStartResult
    | RecipeStopResult
    | RecipeUninstallResult
    | RecipeModelCleanupResult
    | RecipeBuildEvidence
    | RecipeImageImportEvidence
    | RecipeJobRunResult
    | ArtifactDistributionResult
    | AgentFailureResult
    | AgentUpgradeResult
)
PAYLOAD_MODELS: dict[AgentOperation, type[BaseModel]] = {
    AgentOperation.AGENT_UPGRADE: AgentUpgradePayload,
    AgentOperation.ARTIFACT_DISTRIBUTION: ArtifactDistributionPayload,
    AgentOperation.RECIPE_BUILD: RecipeBuildRequest,
    AgentOperation.RECIPE_IMAGE_IMPORT: RecipeImageImportRequest,
    AgentOperation.RECIPE_JOB_RUN: RecipeJobRunRequest,
    AgentOperation.RECIPE_INSTALL: RecipeInstallPayload,
    AgentOperation.RECIPE_START: RecipeStartPayload,
    AgentOperation.RECIPE_STOP: RecipeStopPayload,
    AgentOperation.RECIPE_UNINSTALL: RecipeUninstallPayload,
    AgentOperation.RECIPE_MODEL_UNINSTALL: RecipeModelCleanupPayload,
}

# Result validation is contextual because the result envelope deliberately
# carries no operation discriminator.  Keep this registry alongside the
# payload registry so Controller ingress can resolve the stored operation and
# validate the exact result graph before accepting it.
RESULT_MODELS: dict[AgentOperation, type[BaseModel]] = {
    AgentOperation.AGENT_UPGRADE: AgentUpgradeResult,
    AgentOperation.ARTIFACT_DISTRIBUTION: ArtifactDistributionResult,
    AgentOperation.RECIPE_INSTALL: AgentInstallResult,
    AgentOperation.RECIPE_START: RecipeStartResult,
    AgentOperation.RECIPE_STOP: RecipeStopResult,
    AgentOperation.RECIPE_UNINSTALL: RecipeUninstallResult,
    AgentOperation.RECIPE_MODEL_UNINSTALL: RecipeModelCleanupResult,
    AgentOperation.RECIPE_BUILD: RecipeBuildEvidence,
    AgentOperation.RECIPE_IMAGE_IMPORT: RecipeImageImportEvidence,
    AgentOperation.RECIPE_JOB_RUN: RecipeJobRunResult,
}


def validate_result_for_operation(
    operation: AgentOperation | str,
    result: Any,
    *,
    state: str,
) -> BaseModel | None:
    """Validate a result against the operation stored by the Controller.

    The generic evidence branch remains available for operation kinds whose
    producer has no shared result model.  A current operation with a typed
    result model must pass that model, preventing the generic branch from
    silently accepting malformed known-operation evidence.
    """

    try:
        operation_kind = AgentOperation(operation)
    except (TypeError, ValueError) as error:
        raise AgentProtocolError("agent result operation is invalid") from error
    try:
        model = RESULT_MODELS[operation_kind]
    except KeyError as error:
        raise AgentProtocolError(
            f"result model is not registered for {operation_kind.value}"
        ) from error
    if state != "succeeded":
        return None
    try:
        return model.model_validate_json(canonical_message(result))
    except (TypeError, ValueError, ValidationError) as error:
        raise AgentProtocolError(
            f"{operation_kind.value} result does not match its typed model"
        ) from error


class _ProtocolEnvelopeModel(WireModel):
    """Wire envelope base with the derived extension-map security schema."""

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        document = super().model_json_schema(*args, **kwargs)
        _add_protocol_schema_constraints(document)
        return document


class AgentClaim(_ProtocolEnvelopeModel):
    schema_version: Literal[1]
    job_id: CanonicalUUID
    operation_id: CanonicalUUID
    attempt: int = Field(strict=True, ge=1)
    fence: CanonicalUUID
    node_id: NodeIdentifier
    operation: AgentOperation
    authority_revision: DigestText
    payload_digest: DigestText
    payload: AgentPayload
    deadline: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_wire_scalars(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        document = dict(value)
        if "operation" in document and not isinstance(document["operation"], AgentOperation):
            try:
                document["operation"] = AgentOperation(document["operation"])
            except (TypeError, ValueError):
                pass
        operation = document.get("operation")
        payload = document.get("payload")
        expected_model = PAYLOAD_MODELS.get(operation)
        if expected_model is not None and isinstance(payload, Mapping):
            document["payload"] = expected_model.model_validate(payload)
        if "deadline" in document:
            document["deadline"] = _deadline(document["deadline"])
        return document

    @model_validator(mode="after")
    def validate_wire(self) -> AgentClaim:
        _uuid(self.job_id, name="job_id")
        _uuid(self.operation_id, name="operation_id")
        _attempt(self.attempt)
        _uuid(self.fence, name="fence")
        _node_id(self.node_id)
        expected_model = PAYLOAD_MODELS.get(self.operation)
        if expected_model is None or not isinstance(self.payload, expected_model):
            raise AgentProtocolError(
                f"payload model is not registered for {self.operation.value}"
            )
        payload_document = json.loads(canonical_message(self.payload))
        maximum_bytes = (
            MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
            if self.operation in {AgentOperation.RECIPE_INSTALL, AgentOperation.RECIPE_START}
            else MAX_DOCUMENT_BYTES
        )
        payload = _validate_bounded_document(
            payload_document,
            name="payload",
            operation=self.operation,
            maximum_bytes=maximum_bytes,
        )
        if hashlib.sha256(canonical_message(payload)).hexdigest() != self.payload_digest:
            raise AgentProtocolError("payload digest does not match payload")
        object.__setattr__(self, "payload", self.payload)
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        return self

    @classmethod
    def parse(cls, raw: Any) -> AgentClaim:
        try:
            if isinstance(raw, Mapping) and isinstance(raw.get("payload"), Mapping):
                operation = raw.get("operation")
                try:
                    operation_kind = AgentOperation(operation)
                except (TypeError, ValueError):
                    operation_kind = None
                _validate_bounded_document(
                    raw["payload"],
                    name="payload",
                    operation=operation_kind,
                    maximum_bytes=(
                        MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
                        if operation_kind
                        in {AgentOperation.RECIPE_INSTALL, AgentOperation.RECIPE_START}
                        else MAX_DOCUMENT_BYTES
                    ),
                )
            return cls.model_validate(raw)
        except AgentProtocolError:
            raise
        except ValidationError as error:
            raise AgentProtocolError(str(error)) from error


class AgentProgress(_ProtocolEnvelopeModel):
    schema_version: Literal[1]
    job_id: CanonicalUUID
    operation_id: CanonicalUUID
    attempt: int = Field(strict=True, ge=1)
    fence: CanonicalUUID
    node_id: NodeIdentifier
    deadline: datetime
    progress: OperationProgress

    @model_validator(mode="before")
    @classmethod
    def normalize_wire_scalars(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        document = dict(value)
        if "deadline" in document:
            document["deadline"] = _deadline(document["deadline"])
        if "progress" in document and isinstance(document["progress"], Mapping):
            # Canonical JSON is the one adapter needed to thaw durable mappings.
            document["progress"] = json.loads(canonical_message(document["progress"]))
        return document

    @model_validator(mode="after")
    def validate_wire(self) -> AgentProgress:
        _uuid(self.job_id, name="job_id")
        _uuid(self.operation_id, name="operation_id")
        _attempt(self.attempt)
        _uuid(self.fence, name="fence")
        _node_id(self.node_id)
        _deadline(self.deadline)
        object.__setattr__(
            self,
            "progress",
            OperationProgress.model_validate(
                json.loads(canonical_message(self.progress)), strict=True
            ),
        )
        return self

    @classmethod
    def parse(cls, raw: Any) -> AgentProgress:
        try:
            return cls.model_validate(raw)
        except AgentProtocolError:
            raise
        except ValidationError as error:
            raise AgentProtocolError(str(error)) from error


class AgentDirective(_ProtocolEnvelopeModel):
    """Authenticated heartbeat response for deadline renewal and cancellation."""

    schema_version: Literal[1]
    job_id: CanonicalUUID
    operation_id: CanonicalUUID
    attempt: int = Field(strict=True, ge=1)
    fence: CanonicalUUID
    node_id: NodeIdentifier
    deadline: datetime
    cancel_requested: bool

    @model_validator(mode="before")
    @classmethod
    def normalize_wire_scalars(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        document = dict(value)
        if "deadline" in document:
            document["deadline"] = _deadline(document["deadline"])
        return document

    @model_validator(mode="after")
    def validate_wire(self) -> AgentDirective:
        _uuid(self.job_id, name="job_id")
        _uuid(self.operation_id, name="operation_id")
        _attempt(self.attempt)
        _uuid(self.fence, name="fence")
        _node_id(self.node_id)
        _deadline(self.deadline)
        return self

    @classmethod
    def parse(cls, raw: Any) -> AgentDirective:
        try:
            return cls.model_validate(raw)
        except AgentProtocolError:
            raise
        except ValidationError as error:
            raise AgentProtocolError(str(error)) from error


class AgentResult(_ProtocolEnvelopeModel):
    schema_version: Literal[1]
    job_id: CanonicalUUID
    operation_id: CanonicalUUID
    attempt: int = Field(strict=True, ge=1)
    fence: CanonicalUUID
    node_id: NodeIdentifier
    deadline: datetime
    state: Literal["succeeded", "failed", "cancelled", "waiting-for-operator"]
    result: AgentResultPayload

    @model_validator(mode="before")
    @classmethod
    def normalize_wire_scalars(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        document = dict(value)
        if "deadline" in document:
            document["deadline"] = _deadline(document["deadline"])
        return document

    @model_validator(mode="after")
    def validate_wire(self) -> AgentResult:
        _uuid(self.job_id, name="job_id")
        _uuid(self.operation_id, name="operation_id")
        _attempt(self.attempt)
        _uuid(self.fence, name="fence")
        _node_id(self.node_id)
        _deadline(self.deadline)
        result_document = json.loads(canonical_message(self.result))
        object.__setattr__(
            self,
            "result",
            self.result,
        )
        _validate_bounded_document(
            result_document,
            name="result",
            typed_result_strings=True,
        )
        return self

    @classmethod
    def parse(cls, raw: Any) -> AgentResult:
        try:
            if isinstance(raw, Mapping) and isinstance(raw.get("result"), Mapping):
                _validate_bounded_document(
                    raw["result"],
                    name="result",
                    typed_result_strings=True,
                )
            return cls.model_validate(raw)
        except AgentProtocolError:
            raise
        except ValidationError as error:
            raise AgentProtocolError(str(error)) from error

def schema_validator(schema_name: str) -> Draft202012Validator:
    """Return the package-mandated Draft 2020-12 validator for a wire schema."""
    if schema_name not in {
        "agent-job.schema.json",
        "agent-result.schema.json",
        "agent-directive.schema.json",
        "recipe-job-run.schema.json",
        "telemetry-report.schema.json",
    }:
        raise AgentProtocolError(f"unknown protocol schema: {schema_name}")
    registry: dict[str, type[BaseModel]] = {
        "agent-job.schema.json": AgentClaim,
        "agent-result.schema.json": AgentResult,
        "agent-directive.schema.json": AgentDirective,
    }
    if schema_name == "recipe-job-run.schema.json":
        # Recipe job wire shape is generated from the Pydantic model graph.
        # Keep the import local because recipe_jobs imports these primitives.
        from .recipe_jobs import RecipeJobRunRequest

        registry[schema_name] = RecipeJobRunRequest
    if schema_name == "telemetry-report.schema.json":
        # Telemetry is registered from the same Pydantic model used by the
        # parser. The packaged JSON schema is an export artifact.
        from .telemetry import TelemetryRequest

        registry[schema_name] = TelemetryRequest
    document = registry[schema_name].model_json_schema()
    return Draft202012Validator(document, format_checker=PROTOCOL_FORMAT_CHECKER)


def _add_protocol_schema_constraints(document: dict[str, Any]) -> None:
    """Add recursive boundary constraints to schemas derived from WireModel.

    Pydantic describes the recursive JSON value types, while these two
    constraints are security semantics shared by every extension map. Keeping
    them attached during registry derivation makes ``schema_validator`` and
    the runtime parser enforce the same boundary without a second schema file.
    """

    safe_property_names = {
        "allOf": [
            {"not": {"pattern": UNSAFE_SCHEMA_KEY_PATTERN}},
            {"not": {"pattern": PATH_KEY.pattern}},
        ]
    }
    json_value = document.get("$defs", {}).get("JsonValue")
    if isinstance(json_value, Mapping):
        for branch in json_value.get("anyOf", ()):
            if isinstance(branch, dict) and branch.get("type") == "object":
                branch["propertyNames"] = safe_property_names

    def constrain_extension_objects(value: object) -> None:
        if isinstance(value, dict):
            if (
                value.get("type") == "object"
                and value.get("additionalProperties")
                == {"$ref": "#/$defs/JsonValue"}
            ):
                value["propertyNames"] = safe_property_names
            for child in value.values():
                constrain_extension_objects(child)
        elif isinstance(value, list):
            for child in value:
                constrain_extension_objects(child)

    constrain_extension_objects(document)

    definitions = document.get("$defs", {})
    properties = document.get("properties", {})
    for name in ("payload", "result"):
        value = properties.get(name)
        if isinstance(value, dict):
            if isinstance(value.get("anyOf"), list):
                value["oneOf"] = value.pop("anyOf")
            if value.get("type") == "object":
                value["propertyNames"] = safe_property_names
    result = properties.get("result")
    if (
        isinstance(result, dict)
        and result.get("type") == "object"
        and isinstance(json_value, Mapping)
    ):
        # Result strings are operator evidence and cannot carry client paths.
        # Keep claim payload strings broad because typed operation payloads
        # include authorized URLs and platform targets.
        result_schema = deepcopy(json_value)
        result_schema_name = "ResultJsonValue"
        result_schema_text = json.dumps(result_schema)
        result_schema_text = result_schema_text.replace(
            "#/$defs/JsonValue", "#/$defs/ResultJsonValue"
        )
        result_schema = json.loads(result_schema_text)

        def restrict_result_values(value: object) -> None:
            if not isinstance(value, dict):
                return
            if value.get("type") == "string":
                value["not"] = {"pattern": r"[/\\]"}
            if value.get("type") == "object":
                value["propertyNames"] = safe_property_names
            for child in value.values():
                if isinstance(child, dict):
                    restrict_result_values(child)
                elif isinstance(child, list):
                    for item in child:
                        restrict_result_values(item)

        restrict_result_values(result_schema)
        document.setdefault("$defs", {})[result_schema_name] = result_schema
        result["additionalProperties"] = {"$ref": f"#/$defs/{result_schema_name}"}
    deadline = properties.get("deadline")
    if isinstance(deadline, dict):
        deadline["pattern"] = r"(?:Z|\+00:00)$"


def validate_schema_message(schema_name: str, raw: Any) -> Any:
    """Apply the format-aware wire schema and its mandatory runtime limits."""
    parsers = {
        "agent-job.schema.json": AgentClaim.parse,
        "agent-result.schema.json": AgentResult.parse,
        "agent-directive.schema.json": AgentDirective.parse,
    }
    if schema_name == "telemetry-report.schema.json":
        from .telemetry import TelemetryRequest

        parsers[schema_name] = TelemetryRequest.parse
    if schema_name == "recipe-job-run.schema.json":
        from .recipe_jobs import RecipeJobRunRequest

        parsers[schema_name] = RecipeJobRunRequest.parse
    try:
        parser = parsers[schema_name]
    except KeyError as error:
        raise AgentProtocolError(f"unknown protocol schema: {schema_name}") from error
    if schema_name not in {
        "agent-job.schema.json",
        "agent-result.schema.json",
        "agent-directive.schema.json",
        "recipe-job-run.schema.json",
    }:
        errors = list(schema_validator(schema_name).iter_errors(raw))
        if errors:
            raise AgentProtocolError(f"schema validation failed: {errors[0].message}")
    return parser(raw)
