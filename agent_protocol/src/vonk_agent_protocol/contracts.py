from __future__ import annotations

import hashlib
import importlib.resources
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

MAX_DOCUMENT_BYTES = 64 * 1024
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
RECIPE_BUILD_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
RECIPE_BUILD_CAPABILITIES = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "NET_BIND_SERVICE",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
    }
)
MAX_RECIPE_BUILD_STORAGE_BYTES = 16 * 1024**4


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
AGENT_PACKAGE_URL = re.compile(
    r"https://install\.vonkforge\.ai/"
    r"[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}/vonk-forge-agent\.deb\Z"
)


class AgentProtocolError(ValueError):
    """A protocol message is invalid or outside the agent trust boundary."""


class AgentOperation(StrEnum):
    AGENT_UPGRADE = "agent.upgrade.v1"
    NODE_PROBE = "node.probe"
    RELEASE_INSTALL = "release.install"
    WORKLOAD_PREPARE = "workload.prepare"
    WORKLOAD_START = "workload.start"
    WORKLOAD_STOP = "workload.stop"
    WORKLOAD_HEALTH = "workload.health"
    WORKLOAD_VERIFY = "workload.verify"
    RECIPE_BUILD = "recipe.build.v1"
    RECIPE_IMAGE_IMPORT = "recipe.image.import.v1"
    RECIPE_INSTALL = "recipe.install"
    RECIPE_START = "recipe.start"
    RECIPE_JOB_RUN = "recipe.job.run.v1"
    RECIPE_STOP = "recipe.stop"
    RECIPE_UNINSTALL = "recipe.uninstall"
    RECIPE_MODEL_UNINSTALL = "recipe.model-uninstall.v1"


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
            if _is_path_key(key) and not (
                typed_recipe_build_key
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
                typed_result_strings
                and ("/" in value or "\\" in value)
                and _typed_result_string(path, value)
            )
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
) -> Any:
    if not isinstance(value, Mapping):
        raise AgentProtocolError(f"{name} must be a JSON object")
    _validate_safe_keys(
        value,
        operation=operation,
        typed_result_strings=typed_result_strings,
    )
    copied = _canonical_copy(value, name=name)
    if len(canonical_message(copied)) > MAX_DOCUMENT_BYTES:
        raise AgentProtocolError(f"{name} is too large")
    return copied


def _validate_agent_upgrade_payload(value: Mapping[str, Any]) -> None:
    _fields(
        value,
        required={
            "architecture",
            "package_bytes",
            "package_sha256",
            "package_signature",
            "package_url",
            "package_version",
            "schema_version",
            "target_binary_digest",
            "target_build_digest",
        },
    )
    if (
        value["schema_version"] != 1
        or isinstance(value["schema_version"], bool)
        or value["architecture"] != "linux-arm64"
        or not isinstance(value["package_bytes"], int)
        or isinstance(value["package_bytes"], bool)
        or not 1 <= value["package_bytes"] <= 1024**3
        or not isinstance(value["package_sha256"], str)
        or DIGEST.fullmatch(value["package_sha256"]) is None
        or not isinstance(value["package_signature"], str)
        or re.fullmatch(r"[0-9a-f]{128}", value["package_signature"]) is None
        or not isinstance(value["package_url"], str)
        or AGENT_PACKAGE_URL.fullmatch(value["package_url"]) is None
        or not isinstance(value["package_version"], str)
        or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}", value["package_version"])
        is None
        or not isinstance(value["target_binary_digest"], str)
        or DIGEST.fullmatch(value["target_binary_digest"]) is None
        or not isinstance(value["target_build_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["target_build_digest"]) is None
    ):
        raise AgentProtocolError("agent upgrade payload is invalid")


def _validate_recipe_build_payload(value: Mapping[str, Any]) -> None:
    _fields(
        value,
        required={
            "arguments",
            "base_image_storage_bytes",
            "base_images",
            "build_id",
            "build_input_sha256",
            "capabilities",
            "dockerfile",
            "kind",
            "limits",
            "network",
            "options",
            "platform",
            "recipe_content_sha256",
            "recipe_revision_id",
            "schema_version",
            "source_bundle_bytes",
            "source_bundle_sha256",
            "target",
        },
    )
    _version(value["schema_version"])
    if value["kind"] != "recipe.build.v1":
        raise AgentProtocolError("recipe build kind is not supported")
    _uuid(value["build_id"], name="build_id")
    _uuid(value["recipe_revision_id"], name="recipe_revision_id")
    for name in (
        "recipe_content_sha256",
        "source_bundle_sha256",
        "build_input_sha256",
    ):
        if not isinstance(value[name], str) or DIGEST.fullmatch(value[name]) is None:
            raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    _bounded_build_integer(
        value["source_bundle_bytes"],
        name="source_bundle_bytes",
        minimum=1,
        maximum=64 * 1024 * 1024,
    )
    if value["platform"] != "linux/arm64":
        raise AgentProtocolError("recipe build platform must be linux/arm64")
    dockerfile = value["dockerfile"]
    if not isinstance(dockerfile, str) or not _typed_build_string(
        ("dockerfile",), dockerfile
    ):
        raise AgentProtocolError("recipe build Dockerfile is not canonical")
    target = value["target"]
    if target is not None and (
        not isinstance(target, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target) is None
    ):
        raise AgentProtocolError("recipe build target is not canonical")
    capabilities = _build_sequence(
        value["capabilities"], name="capabilities", maximum=11
    )
    if (
        any(
            capability.startswith("SYS_")
            or capability not in RECIPE_BUILD_CAPABILITIES
            for capability in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
    ):
        raise AgentProtocolError("recipe build capabilities are not allowed")

    arguments = _build_sequence(value["arguments"], name="arguments", maximum=64)
    for argument in arguments:
        item = _mapping(argument)
        _fields(item, required={"name", "value"})
        name = item["name"]
        if not isinstance(name, str) or RECIPE_BUILD_NAME.fullmatch(name) is None:
            raise AgentProtocolError("recipe build argument name is not canonical")
        if not _valid_build_scalar(item["value"]):
            raise AgentProtocolError("recipe build argument value is not scalar")

    base_images = _build_sequence(value["base_images"], name="base_images", maximum=8)
    references: set[str] = set()
    for image in base_images:
        item = _mapping(image)
        _fields(item, required={"manifest_digest", "reference"})
        manifest_digest = item["manifest_digest"]
        reference = item["reference"]
        if (
            not isinstance(manifest_digest, str)
            or not isinstance(reference, str)
            or PINNED_OCI_IMAGE.fullmatch(reference) is None
            or reference.rpartition("@")[2] != manifest_digest
        ):
            raise AgentProtocolError("recipe build base image is not exact")
        if reference in references:
            raise AgentProtocolError("recipe build base image is duplicated")
        references.add(reference)

    storage = _bounded_build_integer(
        value["base_image_storage_bytes"],
        name="base_image_storage_bytes",
        minimum=0 if not base_images else 1,
        maximum=MAX_RECIPE_BUILD_STORAGE_BYTES,
    )
    if not base_images and storage != 0:
        raise AgentProtocolError("base image storage requires a declared base")

    network = _mapping(value["network"])
    _fields(network, required={"hosts", "mode"})
    mode = network["mode"]
    if mode not in {"none", "public"}:
        raise AgentProtocolError("recipe build network mode is not supported")
    hosts = _build_sequence(network["hosts"], name="network hosts", maximum=64)
    if (mode == "none" and hosts) or (mode == "public" and not hosts):
        raise AgentProtocolError("recipe build network declaration is inconsistent")
    if any(
        not isinstance(host, str) or not _valid_public_build_host(host)
        for host in hosts
    ):
        raise AgentProtocolError("recipe build network host is not public")

    options = _mapping(value["options"])
    _fields(
        options,
        required={
            "additional_contexts",
            "annotations",
            "environment",
            "format",
            "identity_label",
            "ignorefile",
            "jobs",
            "labels",
            "layer_compression",
            "layer_labels",
            "layers",
            "no_hostname",
            "no_hosts",
            "omit_history",
            "os_features",
            "os_version",
            "shm_bytes",
            "skip_unused_stages",
            "squash",
            "timestamp",
            "unset_environment",
            "unset_labels",
        },
    )
    _validate_recipe_build_options(options)

    limits = _mapping(value["limits"])
    _fields(
        limits,
        required={
            "container_socket",
            "cpu_cores",
            "gpu",
            "host_mounts",
            "memory_bytes",
            "output_bytes",
            "privileged",
            "processes",
            "temporary_bytes",
            "timeout_seconds",
        },
    )
    for name, maximum in (
        ("cpu_cores", 256),
        ("processes", 65_535),
        ("timeout_seconds", 86_400),
    ):
        _bounded_build_integer(limits[name], name=name, minimum=1, maximum=maximum)
    for name in ("memory_bytes", "temporary_bytes", "output_bytes"):
        _bounded_build_integer(
            limits[name],
            name=name,
            minimum=1,
            maximum=MAX_RECIPE_BUILD_STORAGE_BYTES,
        )
    if limits["gpu"] != 0 or isinstance(limits["gpu"], bool):
        raise AgentProtocolError("recipe build GPU authority must be zero")
    if any(
        limits[name] is not False
        for name in ("privileged", "host_mounts", "container_socket")
    ):
        raise AgentProtocolError("recipe build privilege authority must be false")


def _bounded_build_integer(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise AgentProtocolError(f"{name} is outside its signed bound")
    return value


def _validate_recipe_build_options(value: Mapping[str, Any]) -> None:
    metadata_name = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
    environment_name = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")

    contexts = _build_sequence(
        value["additional_contexts"], name="additional_contexts", maximum=16
    )
    context_names: set[str] = set()
    for raw in contexts:
        item = _mapping(raw)
        _fields(item, required={"name", "path"})
        if (
            not isinstance(item["name"], str)
            or RECIPE_BUILD_NAME.fullmatch(item["name"]) is None
            or item["name"] in context_names
            or not isinstance(item["path"], str)
            or not _typed_build_string(
                ("options", "additional_contexts", 0, "path"), item["path"]
            )
        ):
            raise AgentProtocolError("recipe build additional context is not canonical")
        context_names.add(item["name"])

    for field in ("annotations", "labels", "layer_labels"):
        entries = _build_sequence(value[field], name=field, maximum=64)
        names: set[str] = set()
        for raw in entries:
            item = _mapping(raw)
            _fields(item, required={"name", "value"})
            if (
                not isinstance(item["name"], str)
                or metadata_name.fullmatch(item["name"]) is None
                or item["name"] in names
                or not isinstance(item["value"], str)
                or len(item["value"]) > 1024
            ):
                raise AgentProtocolError(f"recipe build {field} is not canonical")
            names.add(item["name"])

    environment = _build_sequence(value["environment"], name="environment", maximum=64)
    environment_names: set[str] = set()
    for raw in environment:
        item = _mapping(raw)
        _fields(item, required={"name", "value"})
        if (
            not isinstance(item["name"], str)
            or environment_name.fullmatch(item["name"]) is None
            or item["name"] in environment_names
            or not _valid_build_scalar(item["value"])
        ):
            raise AgentProtocolError("recipe build environment is not canonical")
        environment_names.add(item["name"])

    if value["format"] not in {"oci", "docker"}:
        raise AgentProtocolError("recipe build format is not supported")
    if value["layer_compression"] not in {"disabled", "gzip"}:
        raise AgentProtocolError("recipe build layer compression is not supported")
    if value["squash"] not in {"none", "new", "all"}:
        raise AgentProtocolError("recipe build squash mode is not supported")
    for field in (
        "identity_label",
        "layers",
        "no_hostname",
        "no_hosts",
        "omit_history",
        "skip_unused_stages",
    ):
        if not isinstance(value[field], bool):
            raise AgentProtocolError(f"recipe build {field} must be boolean")
    ignorefile = value["ignorefile"]
    if ignorefile is not None and (
        not isinstance(ignorefile, str)
        or not _typed_build_string(("options", "ignorefile"), ignorefile)
    ):
        raise AgentProtocolError("recipe build ignorefile is not canonical")
    _bounded_build_integer(value["jobs"], name="jobs", minimum=1, maximum=32)
    _bounded_build_integer(
        value["shm_bytes"], name="shm_bytes", minimum=65_536, maximum=64 * 1024**3
    )
    timestamp = value["timestamp"]
    if timestamp is not None:
        _bounded_build_integer(
            timestamp, name="timestamp", minimum=0, maximum=4_102_444_800
        )
    os_version = value["os_version"]
    if os_version is not None and (
        not isinstance(os_version, str)
        or re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", os_version) is None
    ):
        raise AgentProtocolError("recipe build OS version is not canonical")
    os_features = _build_sequence(value["os_features"], name="os_features", maximum=32)
    if len(set(os_features)) != len(os_features) or any(
        not isinstance(item, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item) is None
        for item in os_features
    ):
        raise AgentProtocolError("recipe build OS features are not canonical")
    for field, pattern in (
        ("unset_environment", environment_name),
        ("unset_labels", metadata_name),
    ):
        entries = _build_sequence(value[field], name=field, maximum=64)
        if len(set(entries)) != len(entries) or any(
            not isinstance(item, str) or pattern.fullmatch(item) is None
            for item in entries
        ):
            raise AgentProtocolError(f"recipe build {field} is not canonical")


def _build_sequence(value: Any, *, name: str, maximum: int) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise AgentProtocolError(f"{name} is not a bounded array")
    return tuple(value)


def _valid_build_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**63) <= value <= 2**63 - 1
    return (
        isinstance(value, str)
        and len(value.encode("utf-8")) <= 1024
        and "\x00" not in value
    )


def _valid_public_build_host(value: str) -> bool:
    lowered = value.lower()
    if (
        not value
        or len(value.encode("utf-8")) > 253
        or value.startswith(".")
        or value.endswith(".")
        or lowered
        in {
            "localhost",
            "localhost.localdomain",
            "metadata",
            "metadata.google.internal",
            "instance-data.ec2.internal",
        }
        or lowered.endswith((".localhost", ".localdomain", ".internal"))
        or any(
            not character.isascii() or not (character.isalnum() or character in ".-")
            for character in value
        )
    ):
        return False
    if not all(character.isdigit() or character == "." for character in value):
        return True
    try:
        first, second, _third, _fourth = ipaddress.IPv4Address(value).packed
    except ipaddress.AddressValueError:
        return False
    return bool(
        first not in {0, 10, 127}
        and not (first == 100 and 64 <= second <= 127)
        and not (first == 169 and second == 254)
        and not (first == 172 and 16 <= second <= 31)
        and not (first == 192 and second == 168)
        and not (first == 198 and 18 <= second <= 19)
        and first < 224
    )


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
    authority_revision: str
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
        if not isinstance(
            self.authority_revision, str
        ) or not AUTHORITY_REVISION.fullmatch(self.authority_revision):
            raise AgentProtocolError(
                "authority_revision must be a 64-character lowercase SHA-256"
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
        if self.operation is AgentOperation.RECIPE_BUILD:
            _validate_recipe_build_payload(payload)
        elif self.operation is AgentOperation.AGENT_UPGRADE:
            _validate_agent_upgrade_payload(payload)
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
                "authority_revision",
                "payload_digest",
                "payload",
                "deadline",
            },
        )
        try:
            operation = AgentOperation(value["operation"])
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("operation is not supported") from error
        authority_revision = value["authority_revision"]
        if not isinstance(authority_revision, str) or not AUTHORITY_REVISION.fullmatch(
            authority_revision
        ):
            raise AgentProtocolError(
                "authority_revision must be a 64-character lowercase SHA-256"
            )
        payload_digest = value["payload_digest"]
        if not isinstance(payload_digest, str) or not DIGEST.fullmatch(payload_digest):
            raise AgentProtocolError("payload_digest must be a lowercase SHA-256")
        return cls(
            **_attempt_fields(value),
            operation=operation,
            authority_revision=authority_revision,
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
class AgentDirective:
    """Authenticated heartbeat response for deadline renewal and cancellation."""

    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    cancel_requested: bool

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
        if not isinstance(self.cancel_requested, bool):
            raise AgentProtocolError("cancel_requested must be a boolean")

    @classmethod
    def parse(cls, raw: Any) -> AgentDirective:
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
                "cancel_requested",
            },
        )
        return cls(
            **_attempt_fields(value),
            cancel_requested=value["cancel_requested"],
        )


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
        if self.state not in {
            "succeeded",
            "failed",
            "cancelled",
            "waiting-for-operator",
        }:
            raise AgentProtocolError("result state is not supported")
        object.__setattr__(
            self,
            "result",
            _validate_bounded_document(
                self.result,
                name="result",
                typed_result_strings=True,
            ),
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
        "recipe-job-run.schema.json",
        "telemetry-report.schema.json",
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
    parsers = {
        "agent-job.schema.json": AgentClaim.parse,
        "agent-result.schema.json": AgentResult.parse,
        "agent-directive.schema.json": AgentDirective.parse,
    }
    if schema_name == "telemetry-report.schema.json":
        from .telemetry import TelemetryReport

        parsers[schema_name] = TelemetryReport.parse
    if schema_name == "recipe-job-run.schema.json":
        from .recipe_jobs import RecipeJobRunRequest

        parsers[schema_name] = RecipeJobRunRequest.parse
    try:
        parser = parsers[schema_name]
    except KeyError as error:
        raise AgentProtocolError(f"unknown protocol schema: {schema_name}") from error
    errors = list(schema_validator(schema_name).iter_errors(raw))
    if errors:
        raise AgentProtocolError(f"schema validation failed: {errors[0].message}")
    return parser(raw)
