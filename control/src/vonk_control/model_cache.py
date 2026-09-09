"""Durable, content-addressed model artifacts stored on the Controller NAS.

This module is deliberately independent from Spark presence.  The database
stores immutable set identities and checkpoints; the NAS root stores the
actual verified bytes.  A cache entry is only complete when every artifact in
its manifest has an on-disk object with the expected length and SHA-256.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import OperationMemberProgress, canonical_message
from vonk_forge_contracts import ModelDefinition, RecipeDefinition
from vonk_forge_contracts.model import ModelReference

from .cached_file_verification import verified_files
from .catalog_revision_contract import read_catalog_document
from .logging import redact_text
from .model_cache_contract import (
    CacheManifest,
    CacheManifestArtifact,
    ModelCacheOperationProgress,
    ModelCacheOperationResponse,
    ModelCacheOperationResult,
    ModelCacheRepairCheckpoint,
    ModelCacheTransfer,
    parse_model_cache_payload,
    parse_model_cache_result,
)
from .model_cache_progress import cache_phase, cache_progress
from .models import (
    CatalogDocumentRevision,
    FleetProfile,
    ModelCacheArtifact,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
)
from .operation_contract import AvailabilityOperationFailure
from .runtime_init import RuntimeSecretError, read_runtime_secret
from .strict_json import serialize_json_value

SCHEMA_VERSION = 2
SOURCE_POLICY = "nas-first"
_DIGEST_LENGTH = 64
_MAX_ARTIFACTS = 1024
_MAX_MANIFEST_BYTES = 1_048_576
_CHUNK_BYTES = 1024 * 1024
_MAX_HTTP_REDIRECTS = 3
_MAX_OPERATION_ATTEMPTS = 3
_MAX_OPERATOR_RETRIES = 3
_DEFAULT_MAX_PARALLEL_DOWNLOADS = 4
_MAX_PARALLEL_DOWNLOADS = 16
_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 300
_MAX_RETRY_HINT_SECONDS = 365 * 24 * 60 * 60
_TRANSFER_CLAIM_SECONDS = 120
_UPSTREAM_CHECK_SECONDS = 8.0
_UPSTREAM_CHECK_WORKERS = 4
_HF_CANONICAL_HOST = "huggingface.co"
_USE_MANIFEST_BYTES = object()
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_WEIGHT_ROLES = frozenset({"model", "weight", "weights"})


class ModelCacheError(RuntimeError):
    """Base error carrying a stable API code and bounded operator detail."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail[:512]
        self.retry_after_seconds = retry_after_seconds
        self.recovery = recovery
        super().__init__(self.detail)


class ModelCacheConflict(ModelCacheError):
    pass


class ModelCacheNotFound(ModelCacheError):
    pass


class ModelCacheResolutionError(ModelCacheError):
    pass


class ModelCacheStorageError(ModelCacheError):
    pass


_TERMINAL_FAILURE_MARKERS = (
    "digest",
    "integrity",
    "credential",
    "auth",
    "permission",
    "denied",
    "revoked",
    "identity_conflict",
)


def _retryable_failure(error: BaseException | str) -> bool:
    """Classify transport uncertainty without retrying identity failures."""

    code = getattr(error, "code", "")
    detail = getattr(error, "detail", str(error))
    text = f"{code} {detail}".casefold()
    if any(marker in text for marker in _TERMINAL_FAILURE_MARKERS):
        return False
    if code in {
        "model_cache.rate_limited",
        "model_cache.source_truncated",
        "model_cache.source_unavailable",
    }:
        return True
    if isinstance(error, httpx.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is int:
            return status == 429 or status >= 500
        return isinstance(error, (httpx.TimeoutException, httpx.ConnectError))
    if isinstance(error, OSError):
        return error.errno in {
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
            errno.EPIPE,
        }
    return any(
        marker in text
        for marker in (
            "source_unavailable",
            "source_truncated",
            "timeout",
            "timed out",
            "connection",
            "network",
            "temporarily",
            "transport",
            "copy",
            "uncertain",
        )
    )


def _retry_after_seconds(headers: Mapping[str, str], *, now: datetime) -> int | None:
    """Parse Retry-After and standard provider rate-limit reset hints."""

    values: list[int] = []
    raw_retry = headers.get("retry-after")
    if raw_retry:
        try:
            if raw_retry.strip().isdigit():
                values.append(max(0, int(raw_retry.strip())))
            else:
                retry_at = datetime.strptime(
                    raw_retry.strip(), "%a, %d %b %Y %H:%M:%S GMT"
                ).replace(tzinfo=UTC)
                values.append(max(0, int((retry_at - now).total_seconds())))
        except (TypeError, ValueError):
            pass
    for name in ("ratelimit-reset", "x-ratelimit-reset"):
        raw_reset = headers.get(name)
        if not raw_reset:
            continue
        try:
            reset = int(raw_reset.strip())
        except (TypeError, ValueError):
            continue
        # Providers use both a delta and a Unix timestamp.  Values near the
        # current epoch are timestamps; small values are delays.
        values.append(max(0, reset - int(now.timestamp())) if reset > 1_000_000_000 else max(0, reset))
    raw_rate_limit = headers.get("ratelimit") or headers.get("RateLimit")
    if raw_rate_limit:
        # The IETF RateLimit draft permits policy parameters such as `RateLimit:
        # "resolvers";r=0;t=123`.  The `t` value is a delta in seconds.
        values.extend(
            int(match.group(1))
            for match in re.finditer(
                r"(?:^|;)\s*t\s*=\s*(\d+)\s*(?=;|$)", raw_rate_limit
            )
        )
    return min(max(values), _MAX_RETRY_HINT_SECONDS) if values else None


def _huggingface_access_url(source: str) -> str:
    """Return a safe model page URL without revision or signed query data."""

    parsed = urlsplit(source)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 1 and "resolve" in parts:
        parts = parts[: parts.index("resolve")]
    if len(parts) < 2:
        return "https://huggingface.co/"
    return "https://huggingface.co/" + "/".join(parts[:2])


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One exact downloadable artifact in a resolved set."""

    key: str
    artifact_id: str
    path: str
    kind: str
    repository: str | None
    source: str
    revision: str | None
    sha256: str
    expected_bytes: int
    roles: tuple[str, ...]
    model_content_sha256: str | None = None

    def identity(self) -> dict[str, object]:
        return {
            "key": self.key,
            "id": self.artifact_id,
            "path": self.path,
            "kind": self.kind,
            "repository": self.repository,
            "source": self.source,
            "revision": self.revision,
            "sha256": self.sha256,
            "download_bytes": self.expected_bytes,
            "roles": list(self.roles),
            "model_content_sha256": self.model_content_sha256,
        }

    def cache_identity(self) -> dict[str, object]:
        """Return only immutable bytes/source identity for cache reuse.

        Model/recipe content digests, roles, and mount selectors are
        provenance or runtime execution facts.  They must remain visible in
        the manifest but cannot make the same selected file bytes download a
        second time.
        """
        return {
            "key": self.key,
            "id": self.artifact_id,
            "path": self.path,
            "kind": self.kind,
            "repository": self.repository,
            "source": self.source,
            "revision": self.revision,
            "sha256": self.sha256,
            "download_bytes": self.expected_bytes,
        }

    @classmethod
    def from_manifest(cls, value: Mapping[str, object]) -> ArtifactSpec:
        try:
            wire = CacheManifestArtifact.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise ModelCacheResolutionError(
                "model_cache.manifest_invalid", "cache manifest artifact is invalid"
            ) from error
        return cls._from_wire(wire)

    @classmethod
    def _from_wire(cls, wire: CacheManifestArtifact) -> ArtifactSpec:
        result = cls(
            key=wire.key,
            artifact_id=wire.id,
            path=wire.path,
            kind=wire.kind,
            repository=wire.repository,
            source=wire.source,
            revision=wire.revision,
            sha256=wire.sha256,
            expected_bytes=wire.download_bytes,
            roles=tuple(wire.roles),
            model_content_sha256=wire.model_content_sha256,
        )
        _validate_artifact(result)
        return result


@dataclass(frozen=True, slots=True)
class ArtifactSetManifest:
    model_content_sha256: str | None
    recipe_revision_sha256: str | None
    model_content_digests: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]
    model_definition_ref: ModelReference | None = None

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "model_content_sha256": self.model_content_sha256,
            "recipe_revision_sha256": self.recipe_revision_sha256,
            "model_definition_ref": (
                None
                if self.model_definition_ref is None
                else self.model_definition_ref.model_dump(mode="json")
            ),
            "model_content_digests": list(self.model_content_digests),
            "artifacts": [item.identity() for item in self.artifacts],
        }

    @property
    def digest(self) -> str:
        return _sha256_json(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """Return the reusable identity, separate from requested provenance."""
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifacts": [item.cache_identity() for item in self.artifacts],
        }

    @property
    def expected_bytes(self) -> int:
        return sum(
            value.expected_bytes
            for _digest, value in _unique_artifacts(self.artifacts).items()
        )

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> ArtifactSetManifest:
        try:
            _reject_non_json_containers(value)
            wire = CacheManifest.model_validate_json(canonical_message(value))
        except (TypeError, ValueError, ValidationError) as error:
            code = (
                "model_cache.schema_unsupported"
                if isinstance(error, ValidationError)
                and any(
                    issue.get("loc") == ("schema_version",)
                    for issue in error.errors()
                )
                else "model_cache.manifest_invalid"
            )
            detail = (
                "cache manifest schema is unsupported"
                if code == "model_cache.schema_unsupported"
                else "cache manifest shape is invalid"
            )
            raise ModelCacheResolutionError(code, detail) from error
        result = cls(
            model_content_sha256=_optional_digest(wire.model_content_sha256),
            recipe_revision_sha256=_optional_digest(wire.recipe_revision_sha256),
            model_content_digests=tuple(wire.model_content_digests),
            artifacts=tuple(
                sorted(
                    (ArtifactSpec._from_wire(item) for item in wire.artifacts),
                    key=lambda item: item.key,
                )
            ),
            model_definition_ref=wire.model_definition_ref,
        )
        _validate_manifest(result)
        return result


def _reject_non_json_containers(value: object) -> None:
    """Keep Python-only tuple values from being normalized into JSON arrays."""

    if isinstance(value, tuple):
        raise TypeError("manifest JSON must use arrays, not tuples")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_json_containers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_json_containers(item)


def _validated_operation_payload(
    operation: ModelCacheOperation,
) -> dict[str, object]:
    """Read and normalize one persisted operation envelope."""

    try:
        parsed = parse_model_cache_payload(operation.kind, operation.payload)
        if hasattr(parsed, "manifest"):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return serialize_json_value(parsed)  # type: ignore[return-value]
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageError(
            "model_cache.payload_invalid",
            "persisted cache operation payload is invalid",
        ) from error


def _validated_operation_progress(operation: ModelCacheOperation) -> ModelCacheOperationProgress:
    """Read one persisted operation progress document through its wire model."""

    try:
        return ModelCacheOperationProgress.model_validate_json(
            canonical_message(operation.progress)
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageError(
            "model_cache.progress_invalid",
            "persisted cache operation progress is invalid",
        ) from error


def _write_operation_payload(kind: str, value: Mapping[str, object]) -> dict[str, object]:
    """Validate and normalize a newly assembled operation envelope."""

    try:
        parsed = parse_model_cache_payload(kind, value)
        if hasattr(parsed, "manifest"):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return serialize_json_value(parsed)  # type: ignore[return-value]
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageError(
            "model_cache.payload_invalid",
            "cache operation payload is invalid",
        ) from error


@dataclass(frozen=True, slots=True)
class StorageSummary:
    total_bytes: int
    free_bytes: int
    reserve_bytes: int
    available_bytes: int
    unique_used_bytes: int
    in_flight_bytes: int
    protected_bytes: int
    reclaimable_bytes: int

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "available_bytes": self.available_bytes,
            "unique_used_bytes": self.unique_used_bytes,
            "in_flight_bytes": self.in_flight_bytes,
            "protected_bytes": self.protected_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
        }


@dataclass(frozen=True, slots=True)
class CacheOperationView:
    id: str
    request_key: str
    kind: str
    state: str
    attempt: int
    artifact_set_sha256: str | None
    plan_digest: str | None
    progress: Mapping[str, object]
    result: ModelCacheOperationResult | None
    last_error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    retryable: bool = False
    failure: Mapping[str, object] | None = None


def _cache_failure(
    code: str, detail: str, *, retryable: bool, recovery: str,
    retry_time: str | None = None, retry_after_seconds: int | None = None,
    required_bytes: int | None = None, free_bytes: int | None = None,
    shortfall_bytes: int | None = None, artifact_key: str | None = None,
) -> dict[str, object]:
    """Translate an exception once, then persist the canonical public contract."""
    semantic_codes = {
        "model_cache.credentials_missing": "access_required",
        "model_cache.credentials_denied": "access_denied",
        "model_cache.credentials_invalid": "credentials_invalid",
        "model_cache.rate_limited": "rate_limited",
        "model_cache.digest_mismatch": "integrity_mismatch",
        "model_cache.source_size_mismatch": "integrity_mismatch",
        "model_cache.capacity": "capacity",
        "model_cache.interrupted": "interrupted",
    }
    recovery_actions = {
        "access_required": ["open_model_access", "configure_hf_token", "check_access_and_resume"],
        "access_denied": ["open_model_access", "check_access_and_resume"],
        "credentials_invalid": ["configure_hf_token", "check_access_and_resume"],
        "resume": ["resume"], "download_again": ["download_again"],
        "retry": ["retry"], "capacity": ["free_space", "resume"],
        "check_access_and_resume": ["check_access_and_resume"], "inspect": ["inspect"],
    }
    return AvailabilityOperationFailure.model_validate({
        "code": semantic_codes.get(code, code), "detail": redact_text(detail)[:512],
        "retryable": retryable, "recovery_actions": recovery_actions[recovery],
        "retry_time": retry_time, "retry_after_seconds": retry_after_seconds,
        "required_bytes": required_bytes, "free_bytes": free_bytes,
        "shortfall_bytes": shortfall_bytes, "artifact_key": artifact_key,
        "log_excerpt": redact_text(detail)[:1024],
    }).model_dump(mode="json")


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        ) from error
    if value != value.lower():
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        )
    return value


def _validate_artifact(value: ArtifactSpec) -> None:
    if (
        not value.key
        or len(value.key) > 256
        or not value.key[0].isalpha()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for character in value.key)
        or not value.artifact_id
        or len(value.artifact_id) > 256
        or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,255}", value.artifact_id) is None
        or not value.path
        or len(value.path) > 512
        or value.path.startswith("/")
        or "\\" in value.path
        or "\x00" in value.path
        or any(part in {"", ".", ".."} for part in value.path.split("/"))
        or value.kind not in {"huggingface.file", "http.file", "file"}
        or len(value.sha256) != _DIGEST_LENGTH
        or value.sha256 != value.sha256.lower()
        or not _is_hex(value.sha256)
        or not isinstance(value.expected_bytes, int)
        or isinstance(value.expected_bytes, bool)
        or value.expected_bytes < 0
        or (
            value.expected_bytes == 0
            and value.sha256 != _EMPTY_SHA256
        )
        or not value.roles
        or len(value.roles) > 32
        or any(not isinstance(role, str) for role in value.roles)
        or len(set(value.roles)) != len(value.roles)
        or any(not role or len(role) > 64 for role in value.roles)
    ):
        raise ModelCacheResolutionError(
            "model_cache.artifact_invalid", "cache artifact identity is invalid"
        )
    if value.expected_bytes == 0 and any(
        role.lower() in _WEIGHT_ROLES for role in value.roles
    ):
        raise ModelCacheResolutionError(
            "model_cache.artifact_invalid",
            "only verified empty support artifacts may have zero bytes",
        )
    if value.kind != "file" and value.revision is None:
        raise ModelCacheResolutionError(
            "model_cache.revision_missing",
            "remote cache artifacts require an immutable revision",
        )
    _validate_source(value.source)


def _validate_manifest(value: ArtifactSetManifest) -> None:
    if len(value.artifacts) < 1 or len(value.artifacts) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionError(
            "model_cache.artifact_count", "cache artifact set count is invalid"
        )
    keys = [item.key for item in value.artifacts]
    if len(keys) != len(set(keys)):
        raise ModelCacheResolutionError(
            "model_cache.artifact_duplicate", "cache artifact keys must be unique"
        )
    _unique_artifacts(value.artifacts)
    if any(not isinstance(item, str) or not _is_hex(item) for item in value.model_content_digests):
        raise ModelCacheResolutionError(
            "model_cache.model_content_digests_invalid", "cache model dependency pins are invalid"
        )
    encoded = json.dumps(value.document(), sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ModelCacheResolutionError(
            "model_cache.manifest_too_large", "cache manifest exceeds the size limit"
        )


def _unique_artifacts(values: Sequence[ArtifactSpec]) -> dict[str, ArtifactSpec]:
    result: dict[str, ArtifactSpec] = {}
    for item in values:
        existing = result.get(item.sha256)
        if existing is not None and existing.expected_bytes != item.expected_bytes:
            raise ModelCacheResolutionError(
                "model_cache.digest_size_conflict",
                "one artifact digest has conflicting sizes",
            )
        result.setdefault(item.sha256, item)
    return result


def _is_hex(value: str) -> bool:
    if not value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_source(source: str) -> None:
    try:
        parsed = urlsplit(source)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionError(
            "model_cache.source_invalid", "cache source URL is invalid"
        ) from error
    if parsed.scheme in {"http", "https"}:
        if (
            not hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or port is not None
        ):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid", "cache source URL is invalid"
            )
        return
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid", "cache file source is invalid"
            )
        return
    raise ModelCacheResolutionError(
        "model_cache.source_invalid", "cache source must use HTTPS, HTTP or file"
    )


def _source_for_catalog_artifact(
    artifact: Mapping[str, object],
) -> tuple[str, str | None]:
    kind = artifact.get("kind")
    repository = artifact.get("repository")
    path = artifact.get("path")
    revision = artifact.get("revision")
    if not isinstance(repository, str) or not repository:
        raise ModelCacheResolutionError(
            "model_cache.source_invalid", "catalog artifact repository is invalid"
        )
    if not isinstance(path, str) or not path:
        raise ModelCacheResolutionError(
            "model_cache.artifact_invalid", "catalog artifact path is invalid"
        )
    if kind == "huggingface.file":
        from urllib.parse import quote

        # Catalog repositories are immutable HTTPS URLs.  Parse and bind the
        # repository path to the one trusted Hugging Face authority before
        # constructing the file URL; accepting the raw URL here would permit
        # catalog metadata to redirect the Controller to another host.
        try:
            parsed = urlsplit(repository)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.source_invalid",
                "catalog Hugging Face repository is invalid",
            ) from error
        if parsed.scheme:
            if (
                parsed.scheme != "https"
                or hostname is None
                or hostname.lower().rstrip(".") != "huggingface.co"
                or port is not None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/")
            ):
                raise ModelCacheResolutionError(
                    "model_cache.source_invalid",
                    "catalog Hugging Face repository must use canonical HTTPS",
                )
            repository_path = parsed.path.strip("/")
        else:
            # Older catalog fixtures store the repository as the immutable
            # Hugging Face ``namespace/name`` identifier.  It is safe to
            # normalize that bounded identifier to the canonical authority;
            # arbitrary URI repositories still take the guarded path above.
            repository_path = repository
        if not _valid_repository(repository_path):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid",
                "catalog Hugging Face repository is invalid",
            )
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise ModelCacheResolutionError(
                "model_cache.revision_invalid",
                "catalog artifact revision is not immutable",
            )
        source = (
            f"https://huggingface.co/{repository_path}/resolve/{revision}/"
            f"{quote(path, safe='/')}"
        )
    elif kind == "http.file":
        source = repository
    else:
        raise ModelCacheResolutionError(
            "model_cache.source_unsupported",
            "catalog artifact source cannot be downloaded by the NAS cache",
        )
    return source, str(revision) if revision is not None else None


def _valid_repository(value: str) -> bool:
    return (
        bool(
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                value,
            )
        )
    )


def _datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _datetime(value).astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    try:
        return _datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError) as error:
        raise ModelCacheConflict(
            "model_cache.cursor_invalid", "cache cursor boundary is invalid"
        ) from error


def _recipe_definition(document: Mapping[str, object] | RecipeDefinition) -> RecipeDefinition:
    if isinstance(document, RecipeDefinition):
        return document
    try:
        return RecipeDefinition.model_validate(document)
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionError(
            "model_cache.recipe_invalid", "canonical recipe definition is invalid"
        ) from error


def _recipe_model_content_digests(
    document: Mapping[str, object] | RecipeDefinition,
) -> list[str]:
    recipe = _recipe_definition(document)
    raw_models = recipe.models
    if not raw_models:
        raise ModelCacheResolutionError(
            "model_cache.recipe_model_missing",
            "canonical recipe does not declare model selections",
        )
    result: list[str] = []
    for selection in raw_models:
        digest = selection.model.content_sha256
        if not isinstance(digest, str) or not _is_hex(digest) or len(digest) != _DIGEST_LENGTH:
            raise ModelCacheResolutionError(
                "model_cache.model_pin_invalid", "canonical recipe model pin is invalid"
            )
        if digest not in result:
            result.append(digest)
    if len(result) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionError("model_cache.dependency_count", "recipe model set is too large")
    return result


def _recipe_model_file_ids(
    document: Mapping[str, object] | RecipeDefinition | None, digest: str
) -> set[str] | None:
    if document is None:
        return None
    recipe = _recipe_definition(document)
    selected: set[str] = set()
    found = False
    for selection in recipe.models:
        if selection.model.content_sha256 != digest:
            continue
        found = True
        selected.update(file.file_id for file in selection.files)
    return selected if found else None


def _canonical_model_artifacts(row: CatalogDocumentRevision) -> list[dict[str, object]]:
    try:
        definition = read_catalog_document(row)
        if not isinstance(definition, ModelDefinition):
            raise TypeError("catalog revision is not a model")
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionError(
            "model_cache.model_definition_invalid", "canonical model definition is invalid"
        ) from error
    repository = definition.source.repository
    revision = definition.source.revision
    result: list[dict[str, object]] = []
    for value in definition.files:
        result.append(
            {
                "id": value.id,
                "path": value.path,
                "kind": "huggingface.file",
                "repository": repository,
                "revision": revision,
                "sha256": value.sha256,
                "download_bytes": value.size_bytes,
                "roles": list(value.roles),
            }
        )
    return result


def _same_model_artifact_identity(
    row: CatalogDocumentRevision, manifest: ArtifactSetManifest
) -> bool:
    """Compare selected file bytes while ignoring revision/editorial facts."""
    try:
        current = {
            str(item["id"]): (str(item["path"]), str(item["sha256"]), int(item["download_bytes"]))
            for item in _canonical_model_artifacts(row)
        }
    except (KeyError, TypeError, ValueError, ModelCacheResolutionError):
        return False
    selected = {
        item.artifact_id: (item.path, item.sha256, item.expected_bytes)
        for item in manifest.artifacts
    }
    return bool(selected) and all(current.get(key) == identity for key, identity in selected.items())


def _model_lineage_signature(
    document: Mapping[str, object] | ModelDefinition,
) -> tuple[object, object, object]:
    """Return the logical model and representation identity from ModelDefinition."""

    model = document if isinstance(document, ModelDefinition) else ModelDefinition.model_validate(document)
    identity = model.identity
    publisher = identity.model.publisher
    slug = identity.model.slug
    variant = identity.variant
    representation = model.format.model_dump(mode="json")
    return (
        (publisher, slug),
        variant,
        json.dumps(representation, sort_keys=True, separators=(",", ":")),
    )


def _supersedes_revision(
    revision: CatalogDocumentRevision, current: CatalogDocumentRevision
) -> bool:
    document = read_catalog_document(revision)
    value = document.supersedes if isinstance(document, ModelDefinition) else None
    return value is not None and value.content_sha256 == current.content_digest


def _revision_identity(row: CatalogDocumentRevision | None) -> dict[str, object] | None:
    if row is None or not isinstance(row.content_digest, str):
        return None
    return ModelReference(
        publisher=row.publisher,
        slug=row.slug,
        content_sha256=row.content_digest,
    ).model_dump(mode="json")


class ModelCacheService:
    """Resolve, download, verify, repair and evict NAS model artifact sets."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        root: Path,
        *,
        reserve_bytes: int = 10 * 1024**3,
        max_parallel_downloads: int = _DEFAULT_MAX_PARALLEL_DOWNLOADS,
        clock: callable | None = None,
        http_client: httpx.Client | None = None,
        fixture_sources: bool = False,
        trusted_source_hosts: Sequence[str] = ("huggingface.co",),
        huggingface_token_path: Path | None = None,
    ) -> None:
        if not isinstance(root, Path):
            root = Path(root)
        if not root.is_absolute() or any(part in {"", ".", ".."} for part in root.parts):
            raise ValueError("model cache root must be an absolute normalized path")
        if root.is_symlink():
            raise ValueError("model cache root must not be a symlink")
        if not isinstance(reserve_bytes, int) or isinstance(reserve_bytes, bool) or reserve_bytes < 0:
            raise ValueError("model cache reserve must be a non-negative integer")
        if (
            not isinstance(max_parallel_downloads, int)
            or isinstance(max_parallel_downloads, bool)
            or not 1 <= max_parallel_downloads <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise ValueError(
                "model cache parallel downloads must be between 1 and 16"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for child in ("objects", "partials", "quarantine", "manifests", "locks"):
            directory = root / child
            if directory.is_symlink():
                raise ValueError("model cache storage directory must not be a symlink")
            directory.mkdir(mode=0o750, exist_ok=True)
        self._sessions = sessions
        self._root = root
        self._reserve_bytes = reserve_bytes
        self._max_parallel_downloads = max_parallel_downloads
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = http_client
        # Local file and caller-supplied HTTP sources are useful for isolated
        # fixture tests, but are never enabled by the production constructor.
        # Production manifests are resolved from trusted catalog rows only.
        self._fixture_sources = fixture_sources
        self._trusted_source_hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in trusted_source_hosts
            if isinstance(host, str) and host.strip()
        )
        self._huggingface_token_path = (
            Path(huggingface_token_path) if huggingface_token_path is not None else None
        )
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._claim_owner = uuid.uuid4().hex
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallel_downloads,
            thread_name_prefix="vonk-model-cache",
        )
        self._upstream_executor = ThreadPoolExecutor(
            max_workers=_UPSTREAM_CHECK_WORKERS, thread_name_prefix="vonk-model-updates"
        )
        self._upstream_slots = threading.BoundedSemaphore(_UPSTREAM_CHECK_WORKERS)
        self._background_operations: dict[str, dict[str, object]] = {}
        self._digest_events: dict[str, threading.Event] = {}
        self._hf_cooldown_until: datetime | None = None
        self._progress_checkpoint_at: dict[str, datetime] = {}

    def close(self) -> None:
        """Stop the Controller-wide transfer pool during service shutdown."""

        self._closed.set()
        # Let active streams observe the shutdown signal and checkpoint before
        # releasing the service. HTTP clients have bounded read timeouts, so
        # this wait is finite while preventing post-shutdown DB/file writes.
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._upstream_executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self._advance_background_operations()
            self._progress_checkpoint_at.clear()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def reserve_bytes(self) -> int:
        return self._reserve_bytes

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[Session]:
        if isinstance(self._sessions, Session):
            yield self._sessions
            if write:
                self._sessions.flush()
            return
        if write:
            with self._sessions.begin() as session:
                yield session
        else:
            with self._sessions() as session:
                yield session

    def resolve_artifact_set(
        self,
        *,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
    ) -> ArtifactSetManifest:
        model_digest = _optional_digest(model_content_sha256)
        recipe_digest = _optional_digest(recipe_revision_sha256)
        if recipe_digest is not None and recipe_revision_id is not None:
            raise ModelCacheResolutionError(
                "model_cache.recipe_identity_ambiguous",
                "recipe revision digest and ID cannot both be supplied",
            )
        if artifacts is not None:
            if not self._fixture_sources:
                raise ModelCacheResolutionError(
                    "model_cache.fixture_sources_forbidden",
                    "caller-supplied artifact sources are only available to fixture services",
                )
            specs = tuple(
                self._artifact_from_input(value, model_content_sha256=model_digest)
                for value in artifacts
            )
            manifest = ArtifactSetManifest(
                model_content_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_content_digests=(() if model_digest is None else (model_digest,)),
                artifacts=tuple(sorted(specs, key=lambda item: item.key)),
            )
            _validate_manifest(manifest)
            return manifest

        if model_digest is None and recipe_digest is None and recipe_revision_id is None:
            raise ModelCacheResolutionError(
                "model_cache.pin_required",
                "an exact model definition or recipe revision is required",
            )
        with self._session() as session:
            recipe_document: Mapping[str, object] | None = None
            recipe_model_digests: list[str] = []
            if recipe_digest is not None or recipe_revision_id is not None:
                recipe_document, _resolved_recipe_id, resolved_recipe_digest = self._recipe_document(
                    session, recipe_digest, recipe_revision_id
                )
                if recipe_digest is not None and resolved_recipe_digest != recipe_digest:
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_revision_missing",
                        "exact recipe revision is not resolved",
                    )
                recipe_digest = resolved_recipe_digest
                recipe_model_digests = _recipe_model_content_digests(recipe_document)
                if not recipe_model_digests:
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_model_missing",
                        "recipe does not bind an exact model definition",
                    )
                if model_digest is not None and model_digest not in recipe_model_digests:
                    raise ModelCacheConflict(
                        "model_cache.pin_mismatch",
                        "recipe and requested model definitions do not match",
                    )
                model_digest = model_digest or recipe_model_digests[0]
            if model_digest is None:
                raise ModelCacheResolutionError(
                    "model_cache.pin_required",
                    "an exact model definition is required after recipe resolution",
                )
            model_rows: dict[str, CatalogDocumentRevision] = {}
            requested_model_digests = (
                recipe_model_digests if recipe_document is not None else [model_digest]
            )
            for digest in requested_model_digests:
                self._collect_model_definitions(session, digest, model_rows)
            specs: list[ArtifactSpec] = []
            model_ref: ModelReference | None = None
            for digest, row in sorted(model_rows.items()):
                if digest == model_digest:
                    model_ref = ModelReference(
                        publisher=row.publisher,
                        slug=row.slug,
                        content_sha256=digest,
                    )
                raw_artifacts = _canonical_model_artifacts(row)
                selected_ids = _recipe_model_file_ids(recipe_document, digest)
                for raw in raw_artifacts:
                    if selected_ids is not None and raw["id"] not in selected_ids:
                        continue
                    specs.append(
                        self._artifact_from_catalog(
                            raw,
                            model_content_sha256=digest,
                        )
                    )
            manifest = ArtifactSetManifest(
                model_content_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_content_digests=tuple(sorted(model_rows)),
                artifacts=tuple(sorted(specs, key=lambda item: item.key)),
                model_definition_ref=model_ref,
            )
        _validate_manifest(manifest)
        return manifest

    def _recipe_document(
        self,
        session: Session,
        digest: str | None,
        revision_id: str | None,
    ) -> tuple[RecipeDefinition, str, str]:
        if revision_id is not None:
            revision = session.get(CatalogDocumentRevision, revision_id)
        else:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.content_digest == digest,
                    CatalogDocumentRevision.state == "active",
                )
            )
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or not isinstance(revision.content_digest, str)
        ):
            raise ModelCacheResolutionError(
                "model_cache.recipe_revision_missing",
                "exact recipe revision is not resolved",
            )
        try:
            recipe = read_catalog_document(revision)
            if not isinstance(recipe, RecipeDefinition):
                raise TypeError("catalog revision is not a recipe")
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.recipe_invalid", "canonical recipe definition is invalid"
            ) from error
        return recipe, revision.id, revision.content_digest

    def _collect_model_definitions(
        self,
        session: Session,
        digest: str,
        rows: dict[str, CatalogDocumentRevision],
        *,
        visiting: set[str] | None = None,
    ) -> None:
        if not isinstance(digest, str) or not _is_hex(digest) or len(digest) != 64:
            raise ModelCacheResolutionError(
                "model_cache.model_pin_invalid", "model dependency pin is invalid"
            )
        if digest in rows:
            if visiting is not None and digest in visiting:
                raise ModelCacheResolutionError(
                    "model_cache.model_dependency_cycle",
                    "canonical model dependency graph contains a cycle",
                )
            return
        active = visiting if visiting is not None else set()
        if digest in active:
            raise ModelCacheResolutionError(
                "model_cache.model_dependency_cycle",
                "canonical model dependency graph contains a cycle",
            )
        active.add(digest)
        row = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest == digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if row is None:
            active.remove(digest)
            raise ModelCacheResolutionError(
                "model_cache.model_definition_missing",
                "exact model definition is not resolved",
            )
        try:
            definition = read_catalog_document(row)
            if not isinstance(definition, ModelDefinition):
                raise TypeError("catalog revision is not a model")
            rows[digest] = row
            for dependency in definition.dependencies:
                self._collect_model_definitions(
                    session,
                    dependency.content_sha256,
                    rows,
                    visiting=active,
                )
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.model_definition_invalid",
                "canonical model definition is invalid",
            ) from error
        finally:
            active.remove(digest)

    def _artifact_from_catalog(
        self,
        value: Mapping[str, object],
        *,
        model_content_sha256: str,
    ) -> ArtifactSpec:
        raw_id = value.get("id")
        raw_path = value.get("path")
        raw_kind = value.get("kind")
        raw_digest = value.get("sha256")
        raw_bytes = value.get("download_bytes")
        roles = value.get("roles")
        if not isinstance(raw_id, str) or not isinstance(raw_path, str) or not isinstance(raw_kind, str):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "catalog artifact identity is incomplete"
            )
        if not isinstance(raw_digest, str) or not isinstance(raw_bytes, int) or not isinstance(roles, list):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "catalog artifact integrity metadata is incomplete"
            )
        if raw_kind != "huggingface.file" and not self._fixture_sources:
            raise ModelCacheResolutionError(
                "model_cache.source_untrusted",
                "production cache downloads require a trusted Hugging Face artifact reference",
            )
        source, revision = _source_for_catalog_artifact(value)
        spec = ArtifactSpec(
            # The file digest/path is the reusable identity.  A model
            # revision digest is retained as provenance below only.
            key=f"artifact-{raw_digest[:12]}-{raw_id}",
            artifact_id=raw_id,
            path=raw_path,
            kind=raw_kind,
            repository=(
                None if value.get("repository") is None else str(value["repository"])
            ),
            source=source,
            revision=revision,
            sha256=raw_digest,
            expected_bytes=raw_bytes,
            roles=tuple(str(role) for role in roles),
            model_content_sha256=model_content_sha256,
        )
        _validate_artifact(spec)
        return spec

    def _artifact_from_input(
        self,
        value: Mapping[str, object],
        *,
        model_content_sha256: str | None,
    ) -> ArtifactSpec:
        try:
            artifact_id = str(value["id"])
            path = str(value["path"])
            kind = str(value["kind"])
            source_value = value.get("source") or value.get("source_uri")
            repository = value.get("repository")
            if (
                source_value is None
                and kind in {"http.file", "file"}
                and isinstance(repository, str)
            ):
                source_value = repository
            source = str(source_value)
            revision = (
                None if value.get("revision") is None else str(value["revision"])
            )
            raw_roles = value["roles"]
            if not isinstance(raw_roles, list):
                raise TypeError
            digest = str(value["sha256"])
            expected_bytes = int(value["download_bytes"])
        except (KeyError, TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "cache artifact input is invalid"
            ) from error
        spec = ArtifactSpec(
            key=(
                f"artifact-{model_content_sha256[:12]}-{artifact_id}"
                if model_content_sha256 is not None
                else f"artifact-input-{artifact_id}"
            ),
            artifact_id=artifact_id,
            path=path,
            kind=kind,
            repository=(None if repository is None else str(repository)),
            source=source,
            revision=revision,
            sha256=digest,
            expected_bytes=expected_bytes,
            roles=tuple(str(role) for role in raw_roles),
            model_content_sha256=model_content_sha256,
        )
        _validate_artifact(spec)
        return spec

    def start_download(
        self,
        *,
        actor: str,
        request_key: str,
        plan_digest: str,
        artifact_set_sha256: str | None = None,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
        interrupt_after_bytes: int | None = None,
    ) -> CacheOperationView:
        request_key = _request_key(request_key)
        requested_plan = _optional_digest(plan_digest)
        if requested_plan is None:
            raise ModelCacheConflict("model_cache.plan_invalid", "download plan digest is invalid")
        manifest = self._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        set_digest = manifest.digest
        # A retry of the same idempotency key must return the original
        # operation even when its partial checkpoint has changed the current
        # preview's remaining-byte estimate.
        with self._lock, self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                existing_payload = _validated_operation_payload(existing)
                if (
                    existing.kind != "download"
                    or existing_payload.get("artifact_set_sha256") != set_digest
                    or existing.plan_digest != requested_plan
                ):
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                return self._operation_view(existing)
        preview = self._download_preview_for_manifest(manifest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "download preview is stale")
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.download_blocked",
                "; ".join(str(item) for item in preview["blockers"]),
            )
        transfer = preview.get("_transfer")
        if not isinstance(transfer, Mapping):
            transfer = self._transfer_state_for_manifest(manifest, force=False)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifact_set_sha256": set_digest,
            "manifest": manifest.document(),
            "plan_digest": requested_plan,
            "transfer": dict(transfer),
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
        }
        payload = _write_operation_payload("download", payload)
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                existing_payload = _validated_operation_payload(existing)
                if (
                    existing.kind != "download"
                    or existing_payload.get("artifact_set_sha256") != set_digest
                    or existing.plan_digest != requested_plan
                ):
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                self._ensure_set(session, manifest)
                now = self._clock()
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="download",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=set_digest,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress=self._progress(
                        manifest,
                        phase="queued",
                        expected_bytes=int(transfer["total_bytes"]),
                    ),
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        if interrupt_after_bytes is not None:
            self._run_download(
                operation_id,
                force=False,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        return self.get_operation(operation_id)

    def _resolve_requested_manifest(
        self,
        *,
        artifact_set_sha256: str | None,
        model_content_sha256: str | None,
        recipe_revision_sha256: str | None,
        recipe_revision_id: str | None,
        artifacts: Sequence[Mapping[str, object]] | None,
    ) -> ArtifactSetManifest:
        requested_set = _optional_digest(artifact_set_sha256)
        if requested_set is not None and artifacts is None and (
            model_content_sha256 is None
            and recipe_revision_sha256 is None
            and recipe_revision_id is None
        ):
            return self._manifest_for_set(requested_set)
        manifest = self.resolve_artifact_set(
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        if requested_set is not None and manifest.digest != requested_set:
            raise ModelCacheConflict(
                "model_cache.pin_mismatch",
                "requested artifact-set identity does not match the resolved pins",
            )
        return manifest

    def download_preview(
        self,
        *,
        artifact_set_sha256: str | None = None,
        model_content_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        manifest = self._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        return self._download_preview_for_manifest(manifest)

    def _download_preview_for_manifest(
        self, manifest: ArtifactSetManifest
    ) -> dict[str, object]:
        already_cached = 0
        for spec in _unique_artifacts(manifest.artifacts).values():
            if self._object_is_verified(spec):
                already_cached += spec.expected_bytes
        transfer = self._transfer_state_for_manifest(manifest, force=False)
        new_bytes = int(transfer["total_bytes"])
        storage = self.storage_summary()
        blockers = []
        if new_bytes > storage.available_bytes:
            blockers.append("insufficient-reserved-storage")
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "download",
            "artifact_set_sha256": manifest.digest,
            "manifest": manifest.document(),
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "source_policy": SOURCE_POLICY,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": manifest.digest,
            "plan_digest": _sha256_json(plan),
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(manifest.artifacts),
            "expected_bytes": manifest.expected_bytes,
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "blockers": blockers,
            "warnings": [],
            "_manifest": manifest,
            "_transfer": transfer,
        }

    def _partial_bytes(self, set_digest: str, spec: ArtifactSpec) -> int:
        """Return only a bounded, reusable partial checkpoint length."""
        partial = self._partial_path(set_digest, spec.sha256)
        try:
            if not partial.is_file() or partial.is_symlink():
                return 0
            size = partial.stat().st_size
        except OSError:
            return 0
        if size < 0 or size > spec.expected_bytes:
            return 0
        if size == spec.expected_bytes and not self._verify_file(partial, spec):
            return 0
        return size

    def _transfer_state_for_manifest(
        self,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
    ) -> dict[str, object]:
        """Create the immutable planned transfer and per-object baselines."""
        artifacts: dict[str, dict[str, int]] = {}
        total_bytes = 0
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            baseline = (
                0
                if force
                else (
                    spec.expected_bytes
                    if self._object_is_verified(spec)
                    else self._partial_bytes(manifest.digest, spec)
                )
            )
            remaining = max(0, spec.expected_bytes - baseline)
            artifacts[digest] = {
                "baseline_bytes": baseline,
                "received_bytes": 0,
                "started_at": _iso(self._clock()),
            }
            total_bytes += remaining
        return {
            "schema_version": SCHEMA_VERSION,
            "total_bytes": total_bytes,
            "artifacts": artifacts,
        }

    @staticmethod
    def _transfer_totals(payload: Mapping[str, object]) -> tuple[int | None, int]:
        transfer = ModelCacheTransfer.model_validate(payload["transfer"])
        return transfer.total_bytes, sum(
            entry.received_bytes for entry in transfer.artifacts.values()
        )

    def _ensure_transfer_state(
        self,
        operation_id: str,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
    ) -> dict[str, object]:
        """Persist a transfer ledger when resuming an older schema-2 row."""
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id, with_for_update=True)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            payload = _validated_operation_payload(operation)
            transfer = payload["transfer"]
            return dict(transfer) if isinstance(transfer, Mapping) else transfer

    def _operation_transfer_snapshot(
        self, operation_id: str
    ) -> tuple[int | None, int]:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            return self._transfer_totals(_validated_operation_payload(operation))

    def _transfer_state_for_operation(self, operation_id: str) -> Mapping[str, object] | None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                return None
            value = _validated_operation_payload(operation).get("transfer")
            return value if isinstance(value, Mapping) else None

    def _ensure_set(
        self,
        session: Session,
        manifest: ArtifactSetManifest,
    ) -> ModelCacheSet:
        set_digest = manifest.digest
        row = session.get(ModelCacheSet, set_digest)
        now = self._clock()
        if row is None:
            row = ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=SCHEMA_VERSION,
                model_content_sha256=manifest.model_content_sha256,
                recipe_revision_sha256=manifest.recipe_revision_sha256,
                manifest=manifest.document(),
                expected_bytes=manifest.expected_bytes,
                verified_bytes=0,
                state="incomplete",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                verified_at=None,
                last_accessed_at=now,
                last_error=None,
            )
            session.add(row)
            session.flush()
        elif row.manifest != manifest.document():
            # The set row keeps the first requested provenance document, while
            # the primary key is the reusable file identity.  A later model
            # or recipe revision may have different notes, capabilities,
            # roles, or requested digests without changing any bytes.
            stored = ArtifactSetManifest.from_document(row.manifest)
            if stored.digest != manifest.digest:
                raise ModelCacheConflict(
                    "model_cache.identity_conflict",
                    "artifact-set digest resolves to different immutable content",
                )
        for spec in manifest.artifacts:
            artifact = session.get(ModelCacheArtifact, spec.sha256)
            if artifact is None:
                artifact = ModelCacheArtifact(
                    sha256=spec.sha256,
                    storage_key=self._object_key(spec.sha256),
                    expected_bytes=spec.expected_bytes,
                    actual_bytes=0,
                    state="missing",
                    verified_at=None,
                    updated_at=now,
                )
                session.add(artifact)
                session.flush()
            elif artifact.expected_bytes != spec.expected_bytes:
                raise ModelCacheConflict(
                    "model_cache.digest_size_conflict",
                    "artifact digest is already bound to a different size",
                )
            membership = session.get(
                ModelCacheSetArtifact,
                {"artifact_set_sha256": set_digest, "artifact_key": spec.key},
            )
            if membership is None:
                session.add(
                    ModelCacheSetArtifact(
                        artifact_set_sha256=set_digest,
                        artifact_key=spec.key,
                        artifact_sha256=spec.sha256,
                        path=spec.path,
                    )
                )
        return row

    def _progress(
        self,
        manifest: ArtifactSetManifest,
        *,
        phase: str,
        completed_artifacts: int = 0,
        downloaded_bytes: int = 0,
        expected_bytes: int | None | object = _USE_MANIFEST_BYTES,
        current_artifact_key: str | None = None,
        transfer: Mapping[str, object] | None = None,
        previous: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "phase": phase,
            "completed_artifacts": completed_artifacts,
            "total_artifacts": len(manifest.artifacts),
            "downloaded_bytes": downloaded_bytes,
            "expected_bytes": (
                manifest.expected_bytes
                if expected_bytes is _USE_MANIFEST_BYTES
                else expected_bytes
            ),
            "current_artifact_key": current_artifact_key,
        }
        members = []
        raw_artifacts = transfer.get("artifacts") if transfer is not None else None
        unique = _unique_artifacts(manifest.artifacts)
        # Progress cannot impose a shard-count limit on installable models.
        # Larger sets retain exact aggregate counters without a partial member list.
        if isinstance(raw_artifacts, Mapping) and len(unique) <= 1024:
            for spec in unique.values():
                entry = raw_artifacts[spec.sha256]
                baseline, received = entry["baseline_bytes"], entry["received_bytes"]
                members.append(OperationMemberProgress(
                    member_id=spec.sha256, object_sha256=spec.sha256,
                    phase={"downloading": "download", "verifying": "verify", "completed": "completed"}.get(phase, phase),
                    completed_bytes=min(spec.expected_bytes, baseline + received),
                    total_bytes=spec.expected_bytes,
                    state="succeeded" if phase == "completed" or baseline == spec.expected_bytes else "running",
                ))
        return cache_progress(document, previous=previous, now=self._clock(), members=members)

    def _run_download(
        self,
        operation_id: str,
        *,
        force: bool,
        interrupt_after_bytes: int | None = None,
    ) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            operation_payload = _validated_operation_payload(operation)
            operation_set_digest = operation.artifact_set_sha256
            manifest = ArtifactSetManifest.from_document(operation_payload["manifest"])
            set_digest = operation_set_digest
        if set_digest is None:
            raise ModelCacheConflict("model_cache.set_missing", "cache operation has no artifact set")
        transfer = self._ensure_transfer_state(
            operation_id, manifest, force=force
        )
        planned_total = transfer.get("total_bytes")
        planned_total = (
            planned_total
            if type(planned_total) is int and planned_total >= 0
            else None
        )
        self._set_operation_state(operation_id, "running")
        completed = 0
        try:
            with self._session(write=True) as session:
                self._ensure_set(
                    session,
                    manifest,
                )
            unique_specs = list(_unique_artifacts(manifest.artifacts).values())
            self._set_operation_progress(
                operation_id,
                manifest,
                phase="verifying" if force else "downloading",
                completed_artifacts=0,
                downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
                expected_bytes=planned_total,
                current_artifact_key=None,
                transfer=transfer,
            )
            # Each background operation has its own SQLAlchemy sessions and
            # digest-specific partial paths.  The single Controller-wide pool
            # bounds concurrent transfer work across all selected operations.
            for spec in unique_specs:
                self._download_one_unique(
                    spec,
                    set_digest,
                    operation_id=operation_id,
                    force=force,
                    interrupt_after_bytes=interrupt_after_bytes,
                )
            # Count logical manifest entries after all unique objects have
            # completed. Shared digests therefore remain one network transfer
            # while every selected file still reaches the complete stage.
            completed = len(manifest.artifacts)
            self._set_operation_progress(
                operation_id,
                manifest,
                phase="completed",
                completed_artifacts=completed,
                downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
                expected_bytes=planned_total,
                current_artifact_key=None,
                transfer=self._transfer_state_for_operation(operation_id),
            )
            with self._session(write=True) as session:
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "cached"
                    row.verified_bytes = manifest.expected_bytes
                    row.verified_at = self._clock()
                    row.updated_at = self._clock()
                    row.last_accessed_at = row.updated_at
                    row.last_error = None
            self._set_operation_state(
                operation_id,
                "succeeded",
                result={
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": set_digest,
                    "coverage": "complete",
                },
            )
        except InterruptedError as error:
            self._finish_partial(operation_id, set_digest, manifest, str(error) or "download interrupted")
        except (ModelCacheError, OSError, httpx.HTTPError, ValueError) as error:
            self._finish_failed(operation_id, set_digest, manifest, error)

    def _download_one_unique(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        while True:
            with self._lock:
                event = self._digest_events.get(spec.sha256)
                owner = event is None
                if owner:
                    event = threading.Event()
                    self._digest_events[spec.sha256] = event
            if owner:
                break
            # A second selected Model/Recipe waits for the first immutable
            # digest transfer, then reuses its verified object. This prevents
            # concurrent writers from sharing a partial path or replacing a
            # valid object out of order.
            event.wait(timeout=_TRANSFER_CLAIM_SECONDS)
            if not force and self._object_is_verified(spec):
                self._mark_artifact_verified(spec, set_digest)
                return
            with self._lock:
                if self._digest_events.get(spec.sha256) is event and event.is_set():
                    self._digest_events.pop(spec.sha256, None)
        try:
            # The shared NAS lock also covers overlapping Controller worker
            # processes; an in-process Event alone cannot deduplicate them.
            with (self._root / "locks" / spec.sha256).open("a+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                self._download_locked(
                    spec, set_digest, operation_id=operation_id, force=force,
                    interrupt_after_bytes=interrupt_after_bytes,
                )
        finally:
            with self._lock:
                current = self._digest_events.pop(spec.sha256, None)
                if current is not None:
                    current.set()

    def _download_locked(
        self, spec: ArtifactSpec, set_digest: str, *, operation_id: str,
        force: bool, interrupt_after_bytes: int | None,
    ) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            assert operation is not None
            checkpoint = (
                ModelCacheRepairCheckpoint.model_validate(
                    _validated_operation_payload(operation)["repair_checkpoint"]
                )
                if force else None
            )
            repaired = checkpoint.completed_objects if checkpoint is not None else []
        if (not force or spec.sha256 in repaired) and self._object_is_verified(spec):
            self._mark_artifact_verified(spec, set_digest)
            return
        self._download_artifact(
            spec,
            set_digest,
            operation_id=operation_id,
            completed_artifacts=0,
            force=force,
            interrupt_after_bytes=interrupt_after_bytes,
        )
        if force:
            with self._session(write=True) as session:
                operation = session.get(ModelCacheOperation, operation_id, with_for_update=True)
                assert operation is not None
                payload = _validated_operation_payload(operation)
                checkpoint = ModelCacheRepairCheckpoint.model_validate(payload["repair_checkpoint"])
                payload["repair_checkpoint"] = ModelCacheRepairCheckpoint(
                    transfer_id=checkpoint.transfer_id,
                    completed_objects=list(dict.fromkeys([*checkpoint.completed_objects, spec.sha256])),
                ).model_dump(mode="json")
                operation.payload = _write_operation_payload("repair", payload)

    def _download_artifact(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        completed_artifacts: int,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        partial_owner = set_digest
        if force:
            with self._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                assert operation is not None
                checkpoint = ModelCacheRepairCheckpoint.model_validate(
                    _validated_operation_payload(operation)["repair_checkpoint"]
                )
                partial_owner = "repair-" + checkpoint.transfer_id
        part = self._partial_path(partial_owner, spec.sha256)
        part.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if part.is_symlink():
            part.unlink(missing_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        if offset > spec.expected_bytes:
            part.unlink(missing_ok=True)
            offset = 0
        if offset:
            # A previous disk error or abrupt exit can leave readable bytes
            # beyond the last successful sync. Make them durable before using
            # their length for resume or publishing an already-complete file.
            with part.open("r+b") as retained:
                os.fsync(retained.fileno())
        received = offset
        if offset == spec.expected_bytes and self._verify_file(part, spec):
            self._publish_object(spec, part)
            self._mark_artifact_verified(spec, set_digest)
            return
        if offset == spec.expected_bytes:
            part.unlink(missing_ok=True)
            received = 0
            offset = 0
        stream, effective_offset, close = self._open_source(spec, offset)
        if effective_offset != offset:
            received = effective_offset
        durable_received = received
        try:
            mode = "ab" if effective_offset else "wb"
            with part.open(mode) as output:
                synced_at = time.monotonic()

                def sync_received() -> None:
                    nonlocal durable_received, synced_at
                    output.flush()
                    os.fsync(output.fileno())
                    durable_received = received
                    synced_at = time.monotonic()

                try:
                    while True:
                        if self._closed.is_set():
                            raise InterruptedError("model cache service is shutting down")
                        if hasattr(stream, "read"):
                            chunk = stream.read(_CHUNK_BYTES)
                        else:
                            chunk = next(stream, b"")
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            chunk = bytes(chunk)
                        next_received = received + len(chunk)
                        if next_received > spec.expected_bytes:
                            raise ModelCacheStorageError(
                                "model_cache.source_size_mismatch",
                                "source returned more bytes than the immutable artifact pin",
                                recovery="download_again",
                            )
                        output.write(chunk)
                        received = next_received
                        if interrupt_after_bytes is not None and received >= interrupt_after_bytes:
                            raise InterruptedError("download interrupted at a durable checkpoint")
                        # Keep reading individual network fragments so shutdown is
                        # observed promptly; batch only durable disk/DB work. The
                        # file object's bounded buffer avoids another model buffer.
                        if (received - durable_received >= _CHUNK_BYTES
                            or time.monotonic() - synced_at >= 1):
                            sync_received()
                            self._checkpoint_artifact(
                                spec, operation_id=operation_id, set_digest=set_digest,
                                actual_bytes=durable_received, state="partial",
                                completed_artifacts=completed_artifacts, force_progress=False,
                            )
                except (OSError, httpx.HTTPError, ModelCacheError):
                    # Preserve even a sub-MiB tail when a source fails. Never
                    # publish its byte count until the sync has succeeded.
                    if received > durable_received:
                        sync_received()
                    raise
                if received > durable_received:
                    sync_received()
        except (OSError, httpx.HTTPError, ModelCacheError):
            self._checkpoint_artifact(
                spec, operation_id=operation_id, set_digest=set_digest,
                actual_bytes=durable_received, state="partial",
                completed_artifacts=completed_artifacts,
            )
            raise
        finally:
            close()
        if received != spec.expected_bytes:
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state="partial",
            )
            raise ModelCacheStorageError(
                "model_cache.source_truncated",
                "source ended before the immutable artifact size",
                recovery="resume",
            )
        self._checkpoint_artifact(
            spec, operation_id=operation_id, set_digest=set_digest,
            actual_bytes=received, state="verifying", completed_artifacts=completed_artifacts,
        )
        if not self._verify_file(part, spec):
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state="corrupt",
            )
            raise ModelCacheStorageError(
                "model_cache.digest_mismatch",
                "downloaded artifact failed SHA-256 verification",
                recovery="download_again",
            )
        self._publish_object(spec, part)
        self._mark_artifact_verified(spec, set_digest)

    def _open_source(
        self, spec: ArtifactSpec, offset: int
    ) -> tuple[object, int, callable]:
        try:
            parsed = urlsplit(spec.source)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageError(
                "model_cache.source_invalid", "cache source URL is invalid"
            ) from error
        if parsed.scheme == "file":
            if not self._fixture_sources:
                raise ModelCacheStorageError(
                    "model_cache.source_untrusted",
                    "production cache downloads cannot read file sources",
                )
            path = Path(unquote(parsed.path))
            if path.is_symlink() or not path.is_file():
                raise ModelCacheStorageError(
                    "model_cache.source_unavailable", "cache file source is unavailable"
                )
            handle = path.open("rb")
            size = path.stat().st_size
            if offset > size:
                handle.close()
                raise ModelCacheStorageError(
                    "model_cache.source_size_mismatch", "cache file source is shorter than its checkpoint"
                )
            handle.seek(offset)
            return handle, offset, handle.close
        if not self._fixture_sources and (
            parsed.scheme != "https"
            or hostname is None
            or port is not None
            or hostname.lower().rstrip(".")
            not in self._trusted_source_hosts
            or _is_private_host(hostname)
        ):
            raise ModelCacheStorageError(
                "model_cache.source_untrusted",
                "production cache downloads require a trusted HTTPS artifact host",
            )
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(30.0),
                trust_env=False,
            )
        elif not self._fixture_sources and getattr(client, "follow_redirects", False):
            raise ModelCacheStorageError(
                "model_cache.redirect_forbidden",
                "production cache HTTP clients must not follow redirects",
            )
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        response = self._open_http_response(client, spec.source, headers)
        effective_offset = offset
        if offset and response.status_code == 200:
            # The server ignored the range request; restart safely rather than
            # appending a complete payload to a checkpoint.
            response.close()
            response = self._open_http_response(client, spec.source, {})
            effective_offset = 0
        if response.status_code == 206:
            content_range = response.headers.get("content-range", "")
            if not content_range.startswith(f"bytes {effective_offset}-"):
                response.close()
                if owns_client:
                    client.close()
                raise ModelCacheStorageError(
                    "model_cache.range_invalid", "cache source returned an invalid byte range"
                )
        return (
            response.iter_bytes(),
            effective_offset,
            lambda: (response.close(), client.close() if owns_client else None),
        )

    def _open_http_response(
        self,
        client: httpx.Client,
        source: str,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        """Open a pinned source, authenticating only the HF authority.

        Hugging Face commonly redirects a resolve URL to a signed CDN URL.
        Redirects are followed manually so an Authorization header is never
        copied to an arbitrary host. A configured token is sent only on the
        canonical authority request; without a token file, the request stays
        anonymous until the authority reports that access is required.
        """
        current_url = source
        try:
            source_host = urlsplit(source).hostname
        except ValueError:
            source_host = None
        source_is_huggingface = _is_hf_authority(source_host)
        authenticated = False
        token: str | None = None
        token_loaded = False
        if source_is_huggingface and self._huggingface_token_path is not None:
            # A configured token is used on the canonical request so gated
            # files do not incur a public anonymous request. It is never
            # copied to a redirect/CDN host.
            token = self._load_huggingface_token()
            token_loaded = True
            # Compose always names the optional normalized secret path. Its
            # absent/empty projection means anonymous public downloads; only
            # an actual provider denial requires account access and a token.
            authenticated = token is not None
        for redirect_count in range(_MAX_HTTP_REDIRECTS + 1):
            request_headers = dict(headers)
            if authenticated and _is_hf_canonical_url(current_url):
                # The token is intentionally constructed only for the
                # canonical Hugging Face authority. It is never sent to CDN
                # redirect hosts, even when they are trusted HF domains.
                request_headers["Authorization"] = f"Bearer {token}"
            request = client.build_request("GET", current_url, headers=request_headers)
            response = client.send(request, stream=True)
            status_code = response.status_code
            if status_code == 429:
                retry_after = _retry_after_seconds(response.headers, now=self._clock())
                response.close()
                raise ModelCacheStorageError(
                    "model_cache.rate_limited",
                    "artifact provider rate limited this download; it will resume automatically",
                    retry_after_seconds=retry_after,
                    recovery="resume",
                )
            if status_code in {401, 403} and _is_hf_canonical_url(current_url):
                if not token_loaded:
                    token = self._load_huggingface_token()
                    token_loaded = True
                if token is not None and not authenticated:
                    response.close()
                    authenticated = True
                    continue
                response.close()
                if authenticated:
                    raise ModelCacheStorageError(
                        "model_cache.credentials_denied",
                        "Hugging Face could not authorize this download; verify account access and token scope at "
                        f"{_huggingface_access_url(source)}; then use Check access and resume",
                        recovery="access_denied",
                    )
                raise ModelCacheStorageError(
                    "model_cache.credentials_missing",
                    "Hugging Face access is required; request access at "
                    f"{_huggingface_access_url(source)} and configure HF_TOKEN_FILE, then use Check access and resume",
                    recovery="access_required",
                )
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ModelCacheStorageError(
                        "model_cache.redirect_forbidden",
                        "cache source redirect did not provide a destination",
                    )
                redirected_url = urljoin(current_url, location)
                if not source_is_huggingface or not _is_allowed_huggingface_redirect(
                    redirected_url
                ):
                    raise ModelCacheStorageError(
                        "model_cache.redirect_forbidden",
                        "cache source redirected outside the trusted Hugging Face authorities",
                    )
                current_url = redirected_url
                continue
            if status_code not in {200, 206}:
                response.close()
                raise ModelCacheStorageError(
                    "model_cache.source_unavailable",
                    f"cache source request failed with status {status_code}",
                )
            return response
        raise ModelCacheStorageError(
            "model_cache.redirect_forbidden",
            "cache source exceeded the trusted Hugging Face redirect limit",
        )

    def _load_huggingface_token(self) -> str | None:
        path = self._huggingface_token_path
        if path is None:
            return None
        try:
            if path.is_symlink():
                raise RuntimeSecretError("Hugging Face credential path is unsafe")
            if not path.exists():
                return None
            if not path.is_file():
                raise RuntimeSecretError("Hugging Face credential path is unsafe")
            if path.stat().st_size == 0:
                return None
            raw = read_runtime_secret(path)
        except (OSError, RuntimeSecretError):
            raise ModelCacheStorageError(
                "model_cache.credentials_invalid",
                "Hugging Face credential file is unavailable; configure HF_TOKEN_FILE",
                recovery="credentials_invalid",
            ) from None
        value = raw.strip()
        if not value:
            return None
        try:
            token = value.decode("ascii")
        except UnicodeDecodeError:
            raise ModelCacheStorageError(
                "model_cache.credentials_invalid",
                "Hugging Face credential file must contain one ASCII bearer token",
                recovery="credentials_invalid",
            ) from None
        if any(character.isspace() for character in token) or "\x00" in token:
            raise ModelCacheStorageError(
                "model_cache.credentials_invalid",
                "Hugging Face credential file must contain one bearer token",
                recovery="credentials_invalid",
            )
        return token

    def _verify_file(self, path: Path, spec: ArtifactSpec) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        return verified_files.verify_path(path, spec.sha256, spec.expected_bytes)

    def _object_is_verified(self, spec: ArtifactSpec) -> bool:
        return self._verify_file(self._object_path(spec.sha256), spec)

    def _manifest_coverage_complete(self, manifest: ArtifactSetManifest) -> bool:
        """Verify every unique object in a manifest, including empty objects."""
        return all(
            self._object_is_verified(spec)
            for spec in _unique_artifacts(manifest.artifacts).values()
        )

    def _publish_object(self, spec: ArtifactSpec, part: Path) -> None:
        if not self._verify_file(part, spec):
            raise ModelCacheStorageError(
                "model_cache.digest_mismatch",
                "cache artifact failed verification",
                recovery="download_again",
            )
        target = self._object_path(spec.sha256)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        # Atomic overwrite preserves both the pathname and already-open
        # readers until the verified replacement is ready. Moving the old
        # object aside first creates an availability gap (and a crash window).
        os.replace(part, target)
        _fsync_directory(target.parent)

    def _checkpoint_artifact(
        self,
        spec: ArtifactSpec,
        *,
        operation_id: str,
        set_digest: str,
        actual_bytes: int,
        state: str,
        completed_artifacts: int = 0,
        force_progress: bool = True,
    ) -> None:
        now = self._clock()
        with self._lock:
            if not force_progress:
                last = self._progress_checkpoint_at.get(operation_id)
                if last is not None and 0 <= (now - last).total_seconds() < 1:
                    return
            # Bound this process-local fast path to the current sampling window.
            self._progress_checkpoint_at = {
                key: value for key, value in self._progress_checkpoint_at.items()
                if 0 <= (now - value).total_seconds() < 1
            }
            with self._session(write=True) as session:
                operation = session.get(ModelCacheOperation, operation_id, with_for_update=True)
                if operation is not None and not force_progress:
                    prior = _validated_operation_progress(operation).measurement
                    if (prior.phase == "download" and prior.observed_at is not None
                        and 0 <= (now - datetime.fromisoformat(prior.observed_at)).total_seconds() < 1):
                        self._progress_checkpoint_at[operation_id] = now
                        return
                artifact = session.get(ModelCacheArtifact, spec.sha256)
                if artifact is not None:
                    artifact.actual_bytes = actual_bytes
                    artifact.state = "partial" if state == "verifying" else state
                    artifact.updated_at = now
                if operation is not None:
                    payload = _validated_operation_payload(operation)
                    manifest = ArtifactSetManifest.from_document(payload["manifest"])
                    raw_transfer = payload.get("transfer")
                    transfer = dict(raw_transfer) if isinstance(raw_transfer, Mapping) else {}
                    raw_artifacts = transfer.get("artifacts")
                    artifacts = dict(raw_artifacts) if isinstance(raw_artifacts, Mapping) else {}
                    raw_entry = artifacts.get(spec.sha256)
                    entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
                    baseline = entry.get("baseline_bytes")
                    baseline = baseline if type(baseline) is int and baseline >= 0 else 0
                    previous_received = entry.get("received_bytes")
                    previous_received = (
                        previous_received
                        if type(previous_received) is int and previous_received >= 0
                        else 0
                    )
                    entry["baseline_bytes"] = baseline
                    entry["received_bytes"] = max(
                        previous_received, max(0, actual_bytes - baseline)
                    )
                    artifacts[spec.sha256] = entry
                    transfer["schema_version"] = SCHEMA_VERSION
                    transfer["artifacts"] = artifacts
                    total = transfer.get("total_bytes")
                    total = total if type(total) is int and total >= 0 else None
                    received = sum(
                        value.get("received_bytes", 0)
                        for value in artifacts.values()
                        if isinstance(value, Mapping)
                        and type(value.get("received_bytes")) is int
                        and value.get("received_bytes", 0) >= 0
                    )
                    payload["transfer"] = transfer
                    claim = payload.get("claim")
                    if isinstance(claim, Mapping) and claim.get("owner") == self._claim_owner:
                        payload["claim"] = dict(claim) | {
                            "expires_at": _iso(now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS))
                        }
                    operation.payload = _write_operation_payload(operation.kind, payload)
                    old_progress = _validated_operation_progress(operation)
                    old_downloaded = old_progress.downloaded_bytes
                    old_completed = old_progress.completed_artifacts
                    # A partial file is an active download checkpoint. Only an
                    # interrupted operation should enter the resumable partial state.
                    operation.state = "running"
                    operation.progress = self._progress(
                        manifest,
                        previous=old_progress.model_dump(mode="json"),
                        phase="downloading" if state == "partial" else "verifying",
                        completed_artifacts=max(old_completed, completed_artifacts),
                        downloaded_bytes=max(old_downloaded, received),
                        expected_bytes=total,
                        current_artifact_key=spec.key,
                        transfer=transfer,
                    )
                    operation.current_artifact_key = spec.key
                    operation.updated_at = now
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "downloading" if state == "partial" else "verifying"
                    row.verified_bytes = self._verified_bytes(session, set_digest)
                    row.updated_at = now
            self._progress_checkpoint_at[operation_id] = now

    def _mark_artifact_verified(self, spec: ArtifactSpec, set_digest: str) -> None:
        now = self._clock()
        with self._lock, self._session(write=True) as session:
            artifact = session.get(ModelCacheArtifact, spec.sha256)
            if artifact is not None:
                artifact.actual_bytes = spec.expected_bytes
                artifact.state = "verified"
                artifact.verified_at = now
                artifact.updated_at = now
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                row.updated_at = now

    def _verified_bytes(self, session: Session, set_digest: str) -> int:
        rows = session.scalars(
            select(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256 == set_digest
            )
        )
        total = 0
        seen: set[str] = set()
        for membership in rows:
            if membership.artifact_sha256 in seen:
                continue
            seen.add(membership.artifact_sha256)
            artifact = session.get(ModelCacheArtifact, membership.artifact_sha256)
            if artifact is not None and artifact.state == "verified":
                total += artifact.expected_bytes
        return total

    def _finish_partial(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        detail: str,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.state = "incomplete"
                row.verified_bytes = self._verified_bytes(session, set_digest)
                row.updated_at = now
                row.last_error = detail[:512]
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is not None:
                operation.state = "partial"
                operation.last_error = detail[:512]
                payload = _validated_operation_payload(operation) | {
                    "failure": _cache_failure(
                        "model_cache.interrupted",
                        f"{detail[:480]}; preserved bytes remain available to resume",
                        retryable=True, recovery="resume",
                    )
                }
                payload.pop("claim", None)
                operation.payload = _write_operation_payload(operation.kind, payload)
                _total, received = self._transfer_totals(payload)
                previous_progress = _validated_operation_progress(operation)
                operation.progress = self._progress(
                    manifest,
                    previous=previous_progress.model_dump(mode="json"),
                    phase="downloading",
                    completed_artifacts=previous_progress.completed_artifacts,
                    downloaded_bytes=received,
                    expected_bytes=_total,
                    current_artifact_key=operation.current_artifact_key,
                    transfer=(
                        payload.get("transfer")
                        if isinstance(payload.get("transfer"), Mapping)
                        else None
                    ),
                )
                operation.progress = cache_phase(operation.progress, "downloading", now, waiting=True)
                operation.updated_at = now

    def _finish_failed(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        error: BaseException,
        failed_artifact_key: str | None = None,
    ) -> None:
        detail = (
            error.detail
            if isinstance(error, ModelCacheError)
            else f"{type(error).__name__}: {str(error)[:400]}"
        )
        detail = redact_text(detail)[:512]
        now = self._clock()
        required_bytes = free_bytes = shortfall_bytes = None
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            try:
                measured_required = manifest.expected_bytes
                measured_free = self.storage_summary().free_bytes
                required_bytes, free_bytes, shortfall_bytes = (
                    measured_required, measured_free, max(0, measured_required - measured_free)
                )
            except (OSError, RuntimeError, ValueError):
                pass
        failure_code = getattr(error, "code", None)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            failure_code = "model_cache.capacity"
        if not isinstance(failure_code, str) or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", failure_code) is None:
            failure_code = "model_cache.operation_failed"
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                all_valid = all(
                    self._object_is_verified(ArtifactSpec.from_manifest(item))
                    for item in manifest.document()["artifacts"]
                )
                row.state = "cached" if all_valid else "needs-repair"
                row.updated_at = now
                row.last_error = detail[:512]
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is not None:
                retryable = _retryable_failure(error)
                operation_payload = _validated_operation_payload(operation)
                raw_retry = operation_payload["retry"]
                retry = dict(raw_retry) if isinstance(raw_retry, Mapping) else {}
                automatic_attempts = retry.get("automatic_attempts")
                automatic_attempts = (
                    automatic_attempts
                    if type(automatic_attempts) is int and automatic_attempts >= 1
                    else int(operation.attempt)
                )
                operator_retries = retry.get("operator_retries")
                operator_retries = (
                    operator_retries
                    if type(operator_retries) is int and operator_retries >= 0
                    else 0
                )
                bounded_retry = retryable and automatic_attempts < _MAX_OPERATION_ATTEMPTS
                operation.state = "queued" if bounded_retry else "failed"
                operation.last_error = detail[:512]
                retry_delay = getattr(error, "retry_after_seconds", None)
                if type(retry_delay) is not int or retry_delay < 0:
                    retry_delay = min(
                        _RETRY_MAX_SECONDS,
                        _RETRY_BASE_SECONDS * (2 ** max(0, automatic_attempts - 1)),
                    )
                next_retry = now + timedelta(seconds=retry_delay)
                retry.update(
                    automatic_attempts=automatic_attempts,
                    operator_retries=operator_retries,
                    next_retry_at=_iso(next_retry) if bounded_retry else None,
                    retry_after_seconds=retry_delay if bounded_retry else None,
                )
                provider_rate_limited = getattr(error, "code", None) == "model_cache.rate_limited"
                if provider_rate_limited and self._manifest_has_huggingface_source(manifest):
                    self._record_huggingface_cooldown(next_retry)
                failure_payload = _cache_failure(
                    failure_code, detail, retryable=retryable,
                    recovery=getattr(error, "recovery", None)
                    or ("capacity" if failure_code == "model_cache.capacity" else None)
                    or ("resume" if bounded_retry else "retry"),
                    retry_time=_iso(next_retry) if bounded_retry or provider_rate_limited else None,
                    retry_after_seconds=retry_delay if bounded_retry or provider_rate_limited else None,
                    required_bytes=required_bytes, free_bytes=free_bytes,
                    shortfall_bytes=shortfall_bytes, artifact_key=failed_artifact_key,
                )
                operation_payload = operation_payload | {
                    "failure": failure_payload,
                    "retry": retry,
                }
                if bounded_retry:
                    # The queued row represents the next bounded attempt.  Its
                    # manifest, plan digest, and transfer ledger remain intact.
                    operation.attempt = int(operation.attempt) + 1
                    retry["automatic_attempts"] = automatic_attempts + 1
                    operation_payload = operation_payload | {"retry": retry}
                    operation.completed_at = None
                else:
                    operation.completed_at = now
                operation_payload.pop("claim", None)
                operation.payload = _write_operation_payload(operation.kind, operation_payload)
                operation.progress = cache_phase(
                    _validated_operation_progress(operation).model_dump(mode="json"),
                    "queued" if bounded_retry else "failed",
                    now,
                )
                operation.updated_at = now

    def retry(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
    ) -> CacheOperationView:
        """Queue one operator retry from the persisted exact cache operation."""

        request_key = _request_key(request_key)
        with self._lock, self._session(write=True) as session:
            previous = session.get(ModelCacheOperation, operation_id, with_for_update=True)
            if previous is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or existing.plan_digest != previous.plan_digest
                    or existing.artifact_set_sha256 != previous.artifact_set_sha256
                ):
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                return self._operation_view(existing)
            failure = self._canonical_failure(previous)
            retryable = (
                isinstance(failure, Mapping)
                and failure.get("retryable") is True
            )
            previous_payload = _validated_operation_payload(previous)
            raw_retry = previous_payload["retry"]
            retry = dict(raw_retry) if isinstance(raw_retry, Mapping) else {}
            operator_retries = retry.get("operator_retries")
            operator_retries = (
                operator_retries
                if type(operator_retries) is int and operator_retries >= 0
                else 0
            )
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != "failed"
                or not retryable
                or operator_retries >= _MAX_OPERATOR_RETRIES
            ):
                raise ModelCacheConflict(
                    "model_cache.operation_not_retryable",
                    "cache operation is not retryable",
                )
            now = self._clock()
            retry.update(automatic_attempts=1, operator_retries=operator_retries + 1)
            payload = previous_payload | {"retry": retry, "retry_of": previous.id}
            previous_progress = _validated_operation_progress(previous)
            operation = ModelCacheOperation(
                request_key=request_key,
                schema_version=2,
                kind=previous.kind,
                state="queued",
                attempt=1,
                artifact_set_sha256=previous.artifact_set_sha256,
                plan_digest=previous.plan_digest,
                payload=_write_operation_payload(previous.kind, payload),
                progress=cache_phase(
                    previous_progress.model_dump(mode="json"), "queued", now
                ),
                actor=actor,
                current_artifact_key=previous.current_artifact_key,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return self._operation_view(operation)

    def check_access_and_resume(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        artifact_set_sha256: str,
        plan_digest: str,
    ) -> CacheOperationView:
        """Recheck terminal HF access, then queue the exact retained transfer.

        Authentication failures are deliberately terminal for automatic
        scheduling.  This action is the explicit operator boundary after the
        configured token file or upstream access has changed.  It never
        rebuilds a manifest or changes the pinned revision.
        """

        request_key = _request_key(request_key)
        requested_set = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert requested_set is not None and requested_plan is not None
        auth_codes = {
            "access_required",
            "access_denied",
            "credentials_invalid",
        }
        with self._lock, self._session(write=True) as session:
            previous = session.get(ModelCacheOperation, operation_id, with_for_update=True)
            if previous is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if (
                previous.artifact_set_sha256 != requested_set
                or previous.plan_digest != requested_plan
            ):
                raise ModelCacheConflict(
                    "model_cache.identity_mismatch",
                    "access recheck identity does not match the persisted operation",
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.id == previous.id:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "access recheck requires a new operator request key",
                    )
                if (
                    existing.kind == previous.kind
                    and existing.artifact_set_sha256 == previous.artifact_set_sha256
                    and existing.plan_digest == previous.plan_digest
                ):
                    return self._operation_view(existing)
                raise ModelCacheConflict(
                    "model_cache.request_key_reused",
                    "request key was already used for another cache operation",
                )
            failure = self._canonical_failure(previous)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != "failed"
                or not isinstance(failure, Mapping)
                or failure.get("code") not in auth_codes
            ):
                raise ModelCacheConflict(
                    "model_cache.access_recheck_unavailable",
                    "the operation does not have a terminal Hugging Face access failure",
                )
            previous_payload = _validated_operation_payload(previous)
            prior_check = previous_payload.get("access_recheck")
            if (
                isinstance(prior_check, Mapping)
                and prior_check.get("request_key") == request_key
            ):
                return self._operation_view(previous)
            manifest = ArtifactSetManifest.from_document(previous_payload["manifest"])
            failed_artifact_key = (
                failure.get("artifact_key")
                if isinstance(failure.get("artifact_key"), str)
                else previous.current_artifact_key
            )

        try:
            self._check_huggingface_access(
                manifest,
                failed_artifact_key=(
                    failed_artifact_key
                    if isinstance(failed_artifact_key, str)
                    else None
                ),
            )
        except (ModelCacheStorageError, httpx.HTTPError, OSError) as error:
            if _retryable_failure(error):
                self._finish_failed(
                    operation_id,
                    requested_set,
                    manifest,
                    error,
                    failed_artifact_key=(
                        failed_artifact_key
                        if isinstance(failed_artifact_key, str)
                        else None
                    ),
                )
                return self.get_operation(operation_id)
            if not isinstance(error, ModelCacheStorageError):
                raise
            safe_detail = redact_text(error.detail)[:512]
            now = self._clock()
            failure_payload = _cache_failure(
                error.code, safe_detail, retryable=False,
                recovery=error.recovery or "check_access_and_resume",
                artifact_key=failed_artifact_key,
            )
            with self._lock, self._session(write=True) as session:
                previous = session.get(ModelCacheOperation, operation_id, with_for_update=True)
                if previous is None:
                    raise ModelCacheNotFound(
                        "model_cache.operation_missing", "cache operation was not found"
                    )
                previous.state = "failed"
                previous.last_error = safe_detail
                previous.completed_at = now
                previous.updated_at = now
                payload = _validated_operation_payload(previous) | {
                    "failure": failure_payload,
                    "access_recheck": {
                        "request_key": request_key,
                        "checked_at": _iso(now),
                        "authorized": False,
                    },
                }
                payload.pop("claim", None)
                previous.payload = _write_operation_payload(previous.kind, payload)
                return self._operation_view(previous)

        now = self._clock()
        with self._lock, self._session(write=True) as session:
            previous = session.get(ModelCacheOperation, operation_id, with_for_update=True)
            if previous is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            payload = _validated_operation_payload(previous)
            payload.pop("failure", None)
            payload.pop("result", None)
            payload.pop("claim", None)
            retry = payload.get("retry")
            retry = dict(retry) if isinstance(retry, Mapping) else {}
            retry.update(automatic_attempts=1, next_retry_at=None, retry_after_seconds=None)
            payload["retry"] = retry
            payload["resume_of"] = previous.id
            payload["access_recheck"] = {
                "request_key": request_key,
                "checked_at": _iso(now),
                "authorized": True,
            }
            total, received = self._transfer_totals(payload)
            prior_progress = _validated_operation_progress(previous)
            progress = self._progress(
                manifest,
                phase="queued",
                completed_artifacts=prior_progress.completed_artifacts,
                downloaded_bytes=received,
                expected_bytes=total,
                current_artifact_key=(
                    previous.current_artifact_key
                    if isinstance(previous.current_artifact_key, str)
                    else None
                ),
                transfer=(
                    payload.get("transfer")
                    if isinstance(payload.get("transfer"), Mapping)
                    else None
                ),
            )
            operation = ModelCacheOperation(
                request_key=request_key,
                schema_version=SCHEMA_VERSION,
                kind=previous.kind,
                state="queued",
                attempt=1,
                artifact_set_sha256=previous.artifact_set_sha256,
                plan_digest=previous.plan_digest,
                payload=_write_operation_payload(previous.kind, payload),
                progress=progress,
                actor=actor,
                current_artifact_key=previous.current_artifact_key,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return self._operation_view(operation)

    def _check_huggingface_access(
        self,
        manifest: ArtifactSetManifest,
        *,
        failed_artifact_key: str | None = None,
    ) -> None:
        unique_specs = _unique_artifacts(manifest.artifacts)
        exact = next(
            (
                spec
                for spec in unique_specs.values()
                if failed_artifact_key in {spec.key, spec.artifact_id}
            ),
            None,
        ) if failed_artifact_key else None
        if exact is not None and _is_hf_canonical_url(exact.source):
            specs = [exact]
        else:
            # A missing key can occur after an interrupted/recovered worker.
            # Probe one representative per model repository rather than every
            # shard while still checking public and gated dependencies.
            by_repository: dict[str, ArtifactSpec] = {}
            for spec in unique_specs.values():
                if _is_hf_canonical_url(spec.source):
                    by_repository.setdefault(_huggingface_access_url(spec.source), spec)
            specs = list(by_repository.values())
        if not specs:
            raise ModelCacheConflict(
                "model_cache.access_recheck_unavailable",
                "the persisted operation has no canonical Hugging Face source to check",
            )
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(30.0),
                trust_env=False,
            )
        try:
            for spec in specs:
                response = self._open_http_response(
                    client, spec.source, {"Range": "bytes=0-0"}
                )
                response.close()
        finally:
            if owns_client:
                client.close()

    def _set_operation_state(
        self,
        operation_id: str,
        state: str,
        *,
        result: Mapping[str, object] | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            if state == "running":
                if operation.state in {"partial", "failed"}:
                    operation.attempt = int(operation.attempt) + 1
                else:
                    operation.attempt = max(1, int(operation.attempt))
            operation.state = state
            if state in {"succeeded", "failed", "cancelled"}:
                operation.progress = cache_phase(
                    _validated_operation_progress(operation).model_dump(mode="json"),
                    "completed" if state == "succeeded" else "failed",
                    now,
                )
            operation.updated_at = now
            payload = _validated_operation_payload(operation)
            if state == "running":
                payload.pop("failure", None)
            if result is not None:
                parsed_result = parse_model_cache_result(operation.kind, result)
                payload["result"] = parsed_result.model_dump(mode="json")
                payload.pop("failure", None)
            if state in {"succeeded", "failed", "cancelled"}:
                payload.pop("claim", None)
            operation.payload = _write_operation_payload(operation.kind, payload)
            if state in {"succeeded", "failed", "cancelled"}:
                operation.completed_at = now

    def _set_operation_progress(
        self,
        operation_id: str,
        manifest: ArtifactSetManifest,
        *,
        phase: str,
        completed_artifacts: int,
        downloaded_bytes: int,
        current_artifact_key: str | None,
        expected_bytes: int | None = None,
        transfer: Mapping[str, object] | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            old_progress = _validated_operation_progress(operation)
            old_downloaded = old_progress.downloaded_bytes
            old_completed = old_progress.completed_artifacts
            operation.progress = self._progress(
                manifest,
                previous=old_progress.model_dump(mode="json"),
                phase=phase,
                completed_artifacts=max(old_completed, completed_artifacts),
                downloaded_bytes=max(old_downloaded, downloaded_bytes),
                expected_bytes=expected_bytes,
                current_artifact_key=current_artifact_key,
                transfer=(
                    transfer
                    if transfer is not None
                    else _validated_operation_payload(operation).get("transfer")
                ),
            )
            operation.current_artifact_key = current_artifact_key
            operation.state = "running"
            operation.updated_at = now

    def get_operation(self, operation_id: str) -> CacheOperationView:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            return self._operation_view(operation)

    def list_operations(self, *, limit: int = 100) -> tuple[CacheOperationView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            rows = session.scalars(
                select(ModelCacheOperation)
                .order_by(ModelCacheOperation.created_at.desc(), ModelCacheOperation.id.desc())
                .limit(limit)
            )
            return tuple(self._operation_view(row) for row in rows)

    def operations_page(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a stable created-at/id ordered page and raw next boundary."""
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheOperation).order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if (
                    _datetime(row.created_at) == boundary_time
                    and row.id == boundary[1]
                ):
                    start = index + 1
                    break
            else:
                raise ModelCacheConflict(
                    "model_cache.cursor_invalid", "operation cursor boundary is stale"
                )
        page = rows[start : start + limit]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "schema_version": SCHEMA_VERSION,
            "operations": tuple(self._operation_view(row) for row in page),
            "total": total,
            "_next_boundary": next_boundary,
        }

    @staticmethod
    def _operation_view(operation: ModelCacheOperation) -> CacheOperationView:
        payload = _validated_operation_payload(operation)
        progress = _validated_operation_progress(operation)
        result = payload.get("result")
        failure = ModelCacheService._canonical_failure(operation)
        view = CacheOperationView(
            id=operation.id,
            request_key=operation.request_key,
            kind=operation.kind,
            state=operation.state,
            attempt=int(operation.attempt),
            artifact_set_sha256=operation.artifact_set_sha256,
            plan_digest=operation.plan_digest,
            progress=serialize_json_value(progress),  # type: ignore[arg-type]
            result=(
                parse_model_cache_result(operation.kind, result)
                if result is not None else None
            ),
            last_error=operation.last_error,
            created_at=_iso(operation.created_at) or "",
            updated_at=_iso(operation.updated_at) or "",
            completed_at=_iso(operation.completed_at),
            retryable=(
                failure is not None and failure["retryable"] is True
            ),
            failure=failure,
        )
        ModelCacheOperationResponse.model_validate(view, from_attributes=True)
        return view

    @staticmethod
    def _canonical_failure(operation: ModelCacheOperation) -> Mapping[str, object] | None:
        """Read the one current persisted failure contract without repair/defaults."""
        raw = _validated_operation_payload(operation).get("failure")
        return (None if raw is None else
                AvailabilityOperationFailure.model_validate(raw).model_dump(mode="json"))


    def resume_operations(self, *, limit: int = 16) -> int:
        """Return durable cache work for the Controller worker to resume.

        Startup must not perform network or disk transfers inline.  The
        worker calls :meth:`tick` after it has claimed its process loop, so an
        API restart only discovers outstanding work here.
        """
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind.in_(
                            ["download", "repair", "evict"]
                        )
                    )
                    .where(
                        ModelCacheOperation.state.in_(
                            ["queued", "running", "partial"]
                        )
                    )
                )
            )
        return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        """Process a bounded batch synchronously for maintenance and tests.

        Production worker dispatch uses :meth:`tick`, which only claims and
        submits work to the Controller-wide bounded pool.  This synchronous
        method intentionally remains available to deterministic maintenance
        callers and fixture tests.
        """
        if not 1 <= limit <= 16:
            raise ValueError("cache worker batch limit is invalid")
        rows = self._claim_operations(limit=limit, respect_backoff=False)
        for operation_id, kind in rows:
            if kind == "evict":
                self._run_eviction(operation_id)
            else:
                self._run_download(operation_id, force=kind == "repair")
        return len(rows)

    def tick(self, *, limit: int | None = None) -> int:
        """Claim and submit due operations without blocking the worker loop."""

        if self._closed.is_set():
            return 0

        # A caller-supplied Session is intentionally retained for synchronous
        # fixture/maintenance use only. Background tasks must obtain isolated
        # sessions from a sessionmaker; SQLAlchemy Session is not thread safe.
        if isinstance(self._sessions, Session):
            return self.run_pending(limit=1)
        requested = self._max_parallel_downloads if limit is None else limit
        if not 1 <= requested <= _MAX_PARALLEL_DOWNLOADS:
            raise ValueError("cache worker batch limit is invalid")
        with self._lock:
            completed = self._advance_background_operations()
            capacity = max(
                0,
                self._max_parallel_downloads
                - sum(
                    len(
                        [
                            future
                            for future in record.get("futures", [])
                            if isinstance(future, Future) and not future.done()
                        ]
                    )
                    for record in self._background_operations.values()
                ),
            )
            if not capacity:
                return completed
            claimed = self._claim_operations(limit=min(requested, capacity), respect_backoff=True)
            for operation_id, kind in claimed:
                if kind == "evict":
                    future = self._executor.submit(self._run_eviction, operation_id)
                    self._background_operations[operation_id] = {
                        "kind": kind,
                        "futures": [future],
                    }
                    continue
                self._schedule_background_download(
                    operation_id,
                    force=kind == "repair",
                    capacity=1,
                )
            # Allocate one transfer to every selected operation first, then
            # round-robin remaining slots. A large Model cannot monopolize the
            # Controller pool while another selected Model waits at zero.
            while True:
                capacity = self._available_transfer_slots()
                if capacity <= 0:
                    break
                progressed = False
                for operation_id in list(self._background_operations):
                    before = self._available_transfer_slots()
                    record = self._background_operations.get(operation_id, {})
                    futures = record.get("futures", []) if isinstance(record, Mapping) else []
                    pending = sum(
                        1
                        for future in futures
                        if isinstance(future, Future) and not future.done()
                    )
                    self._fill_background_slots(operation_id, pending + 1)
                    if self._available_transfer_slots() < before:
                        progressed = True
                    if self._available_transfer_slots() <= 0:
                        break
                if not progressed:
                    break
            return completed + len(claimed)

    def _available_transfer_slots(self) -> int:
        return max(
            0,
            self._max_parallel_downloads
            - sum(
                len(
                    [
                        future
                        for future in record.get("futures", [])
                        if isinstance(future, Future) and not future.done()
                    ]
                )
                for record in self._background_operations.values()
            ),
        )

    def _schedule_background_download(
        self, operation_id: str, *, force: bool, capacity: int
    ) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            manifest = ArtifactSetManifest.from_document(
                _validated_operation_payload(operation)["manifest"]
            )
            set_digest = operation.artifact_set_sha256
        if set_digest is None:
            raise ModelCacheConflict("model_cache.set_missing", "cache operation has no artifact set")
        transfer = self._ensure_transfer_state(operation_id, manifest, force=force)
        planned_total = transfer.get("total_bytes")
        planned_total = planned_total if type(planned_total) is int and planned_total >= 0 else None
        with self._session(write=True) as session:
            self._ensure_set(session, manifest)
        self._set_operation_state(operation_id, "running")
        specs = list(_unique_artifacts(manifest.artifacts).values())
        record: dict[str, object] = {
            "kind": "repair" if force else "download",
            "set_digest": set_digest,
            "manifest": manifest,
            "force": force,
            "specs": specs,
            "next_index": 0,
            "completed": 0,
            "futures": [],
            "future_specs": {},
            "planned_total": planned_total,
        }
        self._background_operations[operation_id] = record
        self._set_operation_progress(
            operation_id,
            manifest,
            phase="verifying" if force else "downloading",
            completed_artifacts=0,
            expected_bytes=planned_total,
            downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
            current_artifact_key=None,
            transfer=transfer,
        )
        self._fill_background_slots(operation_id, capacity)

    def _fill_background_slots(self, operation_id: str, capacity: int) -> None:
        record = self._background_operations.get(operation_id)
        if record is None:
            return
        specs = record["specs"]
        if not isinstance(specs, list):
            return
        futures = record["futures"]
        if not isinstance(futures, list):
            futures = []
            record["futures"] = futures
        pending = sum(1 for future in futures if isinstance(future, Future) and not future.done())
        while pending < capacity and int(record["next_index"]) < len(specs):
            spec = specs[int(record["next_index"])]
            record["next_index"] = int(record["next_index"]) + 1
            future = self._executor.submit(
                self._download_one_unique,
                spec,
                str(record["set_digest"]),
                operation_id=operation_id,
                force=bool(record["force"]),
                interrupt_after_bytes=None,
            )
            futures.append(future)
            future_specs = record.get("future_specs")
            if isinstance(future_specs, dict):
                future_specs[future] = spec.key
            pending += 1

    def _advance_background_operations(self) -> int:
        self._renew_background_claims()
        finished = 0
        for operation_id, record in list(self._background_operations.items()):
            futures = record.get("futures", [])
            if not isinstance(futures, list):
                futures = []
            done = [future for future in futures if isinstance(future, Future) and future.done()]
            future_specs = record.get("future_specs")
            future_specs = future_specs if isinstance(future_specs, dict) else {}
            first_error = record.get("failure")
            for future in done:
                if future in futures:
                    futures.remove(future)
                failed_artifact_key = future_specs.pop(future, None)
                try:
                    future.result()
                except Exception as error:  # noqa: BLE001 - settle failed background transfers durably
                    if first_error is None:
                        first_error = error
                        record["failure"] = error
                        record["failure_artifact_key"] = failed_artifact_key
                    for other in futures:
                        if isinstance(other, Future):
                            other.cancel()
            if first_error is not None:
                # A cancelled Future may still be running. Keep the durable
                # claim and record until every sibling has settled, so a
                # late checkpoint cannot resurrect a failed operation or
                # overwrite its terminal failure payload.
                if futures:
                    continue
                manifest = record["manifest"]
                if isinstance(manifest, ArtifactSetManifest):
                    if isinstance(first_error, InterruptedError):
                        self._finish_partial(
                            operation_id,
                            str(record["set_digest"]),
                            manifest,
                            str(first_error) or "download interrupted",
                        )
                    else:
                        self._finish_failed(
                            operation_id,
                            str(record["set_digest"]),
                            manifest,
                            first_error,
                            failed_artifact_key=(
                                record.get("failure_artifact_key")
                                if isinstance(record.get("failure_artifact_key"), str)
                                else None
                            ),
                        )
                self._background_operations.pop(operation_id, None)
                finished += 1
                continue
            specs = record.get("specs", [])
            if int(record.get("next_index", 0)) >= len(specs) and not futures:
                manifest = record["manifest"]
                if isinstance(manifest, ArtifactSetManifest):
                    self._finish_background_success(
                        operation_id,
                        str(record["set_digest"]),
                        manifest,
                        record.get("planned_total"),
                    )
                self._background_operations.pop(operation_id, None)
                finished += 1
            else:
                capacity = max(
                    0,
                    self._max_parallel_downloads
                    - sum(
                        len(
                            [
                                current
                                for current in item.get("futures", [])
                                if isinstance(current, Future) and not current.done()
                            ]
                        )
                        for item in self._background_operations.values()
                    ),
                )
                pending = sum(
                    1
                    for future in futures
                    if isinstance(future, Future) and not future.done()
                )
                self._fill_background_slots(operation_id, pending + min(1, capacity))
        return finished

    def _renew_background_claims(self) -> None:
        if not self._background_operations:
            return
        now = self._clock()
        with self._session(write=True) as session:
            for operation_id in self._background_operations:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is None:
                    continue
                operation_payload = _validated_operation_payload(operation)
                claim = operation_payload.get("claim")
                if isinstance(claim, Mapping) and claim.get("owner") == self._claim_owner:
                    operation_payload["claim"] = dict(claim) | {
                        "expires_at": _iso(now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS))
                    }
                    operation.payload = _write_operation_payload(operation.kind, operation_payload)
                    operation.updated_at = now

    def _finish_background_success(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        planned_total: object,
    ) -> None:
        self._set_operation_progress(
            operation_id,
            manifest,
            phase="completed",
            completed_artifacts=len(manifest.artifacts),
            downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
            expected_bytes=planned_total if type(planned_total) is int else None,
            current_artifact_key=None,
            transfer=self._transfer_state_for_operation(operation_id),
        )
        now = self._clock()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.state = "cached"
                row.verified_bytes = manifest.expected_bytes
                row.verified_at = now
                row.updated_at = now
                row.last_accessed_at = now
                row.last_error = None
        self._set_operation_state(
            operation_id,
            "succeeded",
            result={
                "schema_version": SCHEMA_VERSION,
                "artifact_set_sha256": set_digest,
                "coverage": "complete",
            },
        )

    def _claim_operations(
        self, *, limit: int, respect_backoff: bool
    ) -> list[tuple[str, str]]:
        now = self._clock()
        claimed: list[tuple[str, str]] = []
        with self._session(write=True) as session:
            if respect_backoff:
                cooldown_rows = list(
                    session.scalars(
                        select(ModelCacheOperation)
                        .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                        .where(ModelCacheOperation.state.in_(["queued", "running", "partial", "failed"]))
                        .order_by(ModelCacheOperation.updated_at.desc())
                        .limit(256)
                    )
                )
                self._refresh_huggingface_cooldown(cooldown_rows, now)
            candidate_ids = list(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(ModelCacheOperation.kind.in_(["download", "repair", "evict"]))
                    .where(ModelCacheOperation.state.in_(["queued", "running", "partial"]))
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                    .limit(max(limit * 4, limit))
                )
            )
            for operation_id in candidate_ids:
                if len(claimed) >= limit:
                    break
                operation = session.scalar(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.id == operation_id,
                        ModelCacheOperation.kind.in_(["download", "repair", "evict"]),
                        ModelCacheOperation.state.in_(["queued", "running", "partial"]),
                    )
                    .with_for_update(skip_locked=True)
                    # The cooldown scan may have cached this row before another
                    # worker committed a claim; inspect the locked database value.
                    .execution_options(populate_existing=True)
                )
                if operation is None:
                    continue
                payload = _validated_operation_payload(operation)
                retry = payload.get("retry")
                retry = dict(retry) if isinstance(retry, Mapping) else None
                if retry is None and operation.kind in {"download", "repair"}:
                    raise ModelCacheStorageError(
                        "model_cache.payload_invalid", "cache retry state is missing"
                    )
                if respect_backoff and operation.kind in {"download", "repair"}:
                    assert retry is not None
                    retry_at = retry.get("next_retry_at")
                    if isinstance(retry_at, str):
                        try:
                            if datetime.fromisoformat(retry_at) > now:
                                continue
                        except ValueError:
                            continue
                    if (
                        self._hf_cooldown_until is not None
                        and self._hf_cooldown_until > now
                        and self._payload_has_huggingface_source(payload)
                    ):
                        continue
                claim = payload.get("claim")
                claim = dict(claim) if isinstance(claim, Mapping) else None
                if claim is not None:
                    owner = claim.get("owner")
                    expires = claim.get("expires_at")
                    active_background = self._background_operations.get(operation.id)
                    if owner == self._claim_owner and active_background is not None:
                        continue
                    if owner != self._claim_owner:
                        try:
                            if isinstance(expires, str) and datetime.fromisoformat(expires) > now:
                                continue
                        except ValueError:
                            pass
                        if operation.state == "running":
                            operation.state = "partial"
                payload["claim"] = {
                    "owner": self._claim_owner,
                    "expires_at": _iso(now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS)),
                }
                operation.payload = _write_operation_payload(operation.kind, payload)
                operation.updated_at = now
                claimed.append((operation.id, operation.kind))
        return claimed

    @staticmethod
    def _payload_has_huggingface_source(payload: Mapping[str, object]) -> bool:
        manifest = ArtifactSetManifest.from_document(payload["manifest"])
        for artifact in manifest.artifacts:
            source = artifact.source
            try:
                host = urlsplit(source).hostname
            except ValueError:
                host = None
            if _is_hf_authority(host):
                return True
        return False

    @classmethod
    def _manifest_has_huggingface_source(cls, manifest: ArtifactSetManifest) -> bool:
        for spec in _unique_artifacts(manifest.artifacts).values():
            try:
                host = urlsplit(spec.source).hostname
            except ValueError:
                continue
            if _is_hf_authority(host):
                return True
        return False

    def _record_huggingface_cooldown(self, until: datetime) -> None:
        with self._lock:
            if self._hf_cooldown_until is None or until > self._hf_cooldown_until:
                self._hf_cooldown_until = until

    def _refresh_huggingface_cooldown(
        self, rows: Sequence[ModelCacheOperation], now: datetime
    ) -> None:
        """Reconstruct HF throttling after restart from durable failure rows."""

        latest = self._hf_cooldown_until
        for operation in rows:
            payload = _validated_operation_payload(operation)
            if not self._payload_has_huggingface_source(payload):
                continue
            failure = self._canonical_failure(operation)
            if failure is None or failure["code"] != "rate_limited":
                continue
            retry_at = failure["retry_time"]
            if retry_at is None:
                continue
            candidate = _datetime(datetime.fromisoformat(retry_at))
            if candidate > now and (latest is None or candidate > latest):
                latest = candidate
        self._hf_cooldown_until = latest if latest is not None and latest > now else None

    def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = self.get_entry(digest)
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "repair",
            "artifact_set_sha256": digest,
            "artifacts": [item["sha256"] for item in entry["artifacts"]],
            "source_policy": SOURCE_POLICY,
        }
        plan_digest = _sha256_json(plan)
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": digest,
            "plan_digest": plan_digest,
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(entry["artifacts"]),
            "current_state": entry["state"],
            "expected_bytes": entry["expected_bytes"],
            "verified_bytes": entry["verified_bytes"],
        }

    def start_repair(
        self,
        *,
        actor: str,
        request_key: str,
        artifact_set_sha256: str,
        plan_digest: str,
    ) -> CacheOperationView:
        digest = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert digest is not None and requested_plan is not None
        preview = self.repair_preview(digest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "repair preview is stale")
        request_key = _request_key(request_key)
        with self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(ModelCacheOperation.request_key == request_key)
            )
            if existing is not None:
                _validated_operation_payload(existing)
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                return self._operation_view(existing)
        manifest = self._manifest_for_set(digest)
        transfer = self._transfer_state_for_manifest(manifest, force=True)
        if int(transfer["total_bytes"]) > self.storage_summary().available_bytes:
            raise ModelCacheConflict(
                "model_cache.download_blocked", "insufficient-reserved-storage"
            )
        payload = {
            "repair_checkpoint": ModelCacheRepairCheckpoint(transfer_id=uuid.uuid4().hex, completed_objects=[]).model_dump(mode="json"),
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifact_set_sha256": digest,
            "manifest": manifest.document(),
            "plan_digest": requested_plan,
            "transfer": transfer,
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
        }
        payload = _write_operation_payload("repair", payload)
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="repair",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=digest,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress=self._progress(
                        manifest,
                        phase="queued",
                        expected_bytes=int(transfer["total_bytes"]),
                    ),
                    actor=actor,
                    created_at=self._clock(),
                    updated_at=self._clock(),
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _manifest_for_set(self, digest: str) -> ArtifactSetManifest:
        with self._session() as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                raise ModelCacheNotFound("model_cache.entry_missing", "cache entry was not found")
            manifest = ArtifactSetManifest.from_document(row.manifest)
            if manifest.digest != digest:
                raise ModelCacheConflict(
                    "model_cache.identity_conflict",
                    "persisted cache manifest does not match its artifact-set identity",
                )
            return manifest

    def manifest_for_artifact_set(self, artifact_set_sha256: str) -> ArtifactSetManifest:
        """Return the persisted immutable manifest for an exact artifact set.

        Consumers that prepare or distribute a model use this boundary rather
        than rebuilding an identity from display metadata.  The persisted
        manifest is re-hashed before it is returned, so a database row with a
        mismatched primary key cannot become a trusted source descriptor.
        """
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        return self._manifest_for_set(digest)

    def preparation_evidence(self, artifact_set_sha256: str) -> dict[str, object]:
        """Project exact model preparation evidence for run/profile adapters.

        The cache owns Controller-side model bytes only.  ``targets`` stays
        empty because target readiness is established by the distribution
        worker after agent-authenticated transfer and verification.
        """
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = self.get_entry(digest)
        manifest = self.manifest_for_artifact_set(digest)
        if manifest.model_content_sha256 is None:
            raise ModelCacheResolutionError(
                "model_cache.model_pin_missing",
                "preparation evidence requires an exact primary model definition",
            )
        dependencies = sorted(
            value
            for value in manifest.model_content_digests
            if value != manifest.model_content_sha256
        )
        expected_bytes = int(entry["expected_bytes"])
        verified_bytes = int(entry["verified_bytes"])
        complete = entry["coverage"] == "complete"
        state = str(entry["state"])
        controller_state = {
            "cached": "ready",
            "incomplete": "preparing",
            "downloading": "preparing",
            "verifying": "verifying",
            "needs-repair": "failed",
            "failed": "failed",
        }.get(state, "unknown")
        reason = None
        if controller_state in {"failed", "unknown"}:
            reason = str(entry.get("last_error") or "model cache is not complete")
        return {
            "artifact_set_sha256": digest,
            "model_content_sha256": manifest.model_content_sha256,
            "recipe_revision_sha256": manifest.recipe_revision_sha256,
            "artifact_count": len(manifest.artifacts),
            "artifact_set_bytes": expected_bytes,
            "dependency_model_content_sha256": dependencies,
            "completeness": "complete" if complete else "incomplete",
            "controller": {
                "state": controller_state,
                "expected_bytes": expected_bytes,
                "verified_bytes": verified_bytes,
                "missing_bytes": max(0, expected_bytes - verified_bytes),
                "verified_sha256": digest if complete else None,
                "verified_at": entry.get("verified_at"),
                "source": "nas-cache",
                "reason": reason,
            },
            "targets": [],
        }

    def activity_operations(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 101,
        state: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, object]:
        """Return cache operations for the global Activity provider seam.

        Cache work is Controller/NAS scoped and therefore has no Spark node
        IDs.  A node filter consequently returns an empty page while still
        reporting the unfiltered-by-cursor total for the requested state.
        ``after`` is the already authenticated global activity boundary.
        """
        if not 1 <= limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        if state is not None and (not isinstance(state, str) or not state.strip()):
            raise ValueError("operation state filter is invalid")
        if node_id is not None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        allowed_states = {
            "queued",
            "running",
            "partial",
            "succeeded",
            "failed",
            "cancelled",
        }
        if state is not None and state not in allowed_states:
            return {"operations": (), "total": 0, "_next_boundary": None}
        with self._session() as session:
            filters = []
            if state is not None:
                filters.append(ModelCacheOperation.state == state)
            if after is not None:
                boundary_time, boundary_id = after
                boundary_time = _datetime(boundary_time)
                filters.append(
                    (ModelCacheOperation.created_at < boundary_time)
                    | (
                        (ModelCacheOperation.created_at == boundary_time)
                        & (ModelCacheOperation.id < boundary_id)
                    )
                )
            rows = list(
                session.scalars(
                    select(ModelCacheOperation)
                    .where(*filters)
                    .order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                    # Fetch one sentinel row so the provider can expose a
                    # stable boundary instead of silently truncating pages.
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            total_filters = []
            if state is not None:
                total_filters.append(ModelCacheOperation.state == state)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(*total_filters)
                )
                or 0
            )
        next_boundary = None
        if has_more and rows:
            last = rows[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "operations": tuple(self._operation_view(row) for row in rows),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def verified_artifact_file(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
    ) -> tuple[Path, int, str]:
        """Return one immutable object only after complete-set verification.

        This is the Controller-to-agent serving seam.  The caller receives a
        content-addressed path and must stream it from the returned file
        descriptor/path; no caller-controlled filesystem path is accepted.
        """
        set_digest = _optional_digest(artifact_set_sha256)
        object_digest = _optional_digest(artifact_sha256)
        if set_digest is None or object_digest is None or not _valid_relative_path(artifact_path):
            raise ModelCacheNotFound(
                "model_cache.artifact_missing", "verified cache artifact was not found"
            )
        manifest = self._manifest_for_set(set_digest)
        if not all(self._object_is_verified(spec) for spec in manifest.artifacts):
            raise ModelCacheConflict(
                "model_cache.coverage_incomplete",
                "cache artifact set is not completely verified",
            )
        spec = next(
            (
                value
                for value in manifest.artifacts
                if value.sha256 == object_digest and value.path == artifact_path
            ),
            None,
        )
        if spec is None:
            raise ModelCacheNotFound(
                "model_cache.artifact_missing", "verified cache artifact was not found"
            )
        path = self._object_path(spec.sha256)
        if path.is_symlink() or not path.is_file() or not self._verify_file(path, spec):
            raise ModelCacheConflict(
                "model_cache.artifact_unverified",
                "cache artifact is no longer verified",
            )
        return path, spec.expected_bytes, spec.sha256

    def resolve_verified_artifact_set(
        self, artifact_set_sha256: str
    ) -> tuple[dict[str, object], ...]:
        """Describe every verified object in a complete immutable set.

        The distribution worker consumes these descriptors to copy one
        content-addressed object to one or more agents.  It never receives a
        source URL or a caller-controlled path from this adapter.
        """
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise ModelCacheNotFound(
                "model_cache.entry_missing", "cache entry was not found"
            )
        manifest = self._manifest_for_set(digest)
        descriptors = []
        for spec in manifest.artifacts:
            path, size, object_digest = self.verified_artifact_file(
                digest, spec.sha256, spec.path
            )
            descriptors.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": digest,
                    "artifact_key": spec.key,
                    "file_id": spec.artifact_id,
                    "model_content_sha256": spec.model_content_sha256,
                    "path": spec.path,
                    "sha256": object_digest,
                    "bytes": size,
                    "storage_key": self._object_key(object_digest),
                    "file": path,
                    "roles": list(spec.roles),
                }
            )
        return tuple(descriptors)

    def read_verified_artifact(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
        *,
        offset: int = 0,
        maximum_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        """Read a bounded range from a complete verified cache set."""
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(maximum_bytes, int)
            or isinstance(maximum_bytes, bool)
            or not 0 < maximum_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("verified artifact read bounds are invalid")
        path, size, _digest = self.verified_artifact_file(
            artifact_set_sha256, artifact_sha256, artifact_path
        )
        if offset >= size:
            return b""
        with path.open("rb") as source:
            source.seek(offset)
            return source.read(min(maximum_bytes, size - offset))

    def get_entry(self, artifact_set_sha256: str) -> dict[str, object]:
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        self.reconcile_storage()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                raise ModelCacheNotFound("model_cache.entry_missing", "cache entry was not found")
            self._refresh_protection(session, row)
            manifest = ArtifactSetManifest.from_document(row.manifest)
            artifacts = []
            unique_bytes = 0
            seen: set[str] = set()
            for spec in manifest.artifacts:
                cache_artifact = session.get(ModelCacheArtifact, spec.sha256)
                state = "missing"
                actual = 0
                if cache_artifact is not None:
                    state = cache_artifact.state
                    actual = cache_artifact.actual_bytes
                    if spec.sha256 not in seen and state == "verified":
                        unique_bytes += spec.expected_bytes
                        seen.add(spec.sha256)
                artifacts.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "key": spec.key,
                        "id": spec.artifact_id,
                        "path": spec.path,
                        "sha256": spec.sha256,
                        "expected_bytes": spec.expected_bytes,
                        "actual_bytes": actual,
                        "roles": list(spec.roles),
                        "state": state,
                        "source": spec.source,
                    }
                )
            update = self._update_flags(session, row, manifest)
            return {
                "schema_version": SCHEMA_VERSION,
                "artifact_set_sha256": digest,
                "model_content_sha256": row.model_content_sha256,
                "recipe_revision_sha256": row.recipe_revision_sha256,
                "state": row.state,
                "coverage": "complete" if row.state == "cached" else "incomplete",
                "expected_bytes": row.expected_bytes,
                "verified_bytes": row.verified_bytes,
                "unique_bytes": unique_bytes,
                "artifacts": artifacts,
                "protected": bool(row.protected),
                "protected_reasons": list(row.protected_reasons or ()),
                "update_available": update[0],
                "recipe_update_available": update[1],
                "created_at": _iso(row.created_at) or "",
                "updated_at": _iso(row.updated_at) or "",
                "verified_at": _iso(row.verified_at),
                "last_error": row.last_error,
            }

    def inventory(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= 100:
            raise ValueError("cache entry limit is invalid")
        self.reconcile_storage()
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheSet)
                    .order_by(ModelCacheSet.updated_at.desc(), ModelCacheSet.artifact_set_sha256.desc())
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if (
                    _datetime(row.updated_at) == boundary_time
                    and row.artifact_set_sha256 == boundary[1]
                ):
                    start = index + 1
                    break
            else:
                raise ModelCacheConflict(
                    "model_cache.cursor_invalid", "cache inventory cursor boundary is stale"
                )
        page = rows[start : start + limit]
        entries = [self.get_entry(row.artifact_set_sha256) for row in page]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "entries": entries,
            "storage": self.storage_summary().document(),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def reconcile_storage(self) -> dict[str, object]:
        with self._lock:
            with self._session(write=True) as session:
                rows = list(session.scalars(select(ModelCacheArtifact)))
                for artifact in rows:
                    previous = (artifact.state, artifact.actual_bytes)
                    path = self._object_path(artifact.sha256)
                    actual = path.stat().st_size if path.exists() and not path.is_symlink() else 0
                    if (
                        actual == artifact.expected_bytes
                        and path.is_file()
                        and not path.is_symlink()
                        and verified_files.verify_path(path, artifact.sha256, artifact.expected_bytes)
                    ):
                        artifact.state = "verified"
                        artifact.actual_bytes = actual
                    else:
                        artifact.state = "missing" if actual == 0 else "corrupt"
                        artifact.actual_bytes = min(actual, artifact.expected_bytes)
                    if previous != (artifact.state, artifact.actual_bytes):
                        artifact.updated_at = self._clock()
                sets = list(session.scalars(select(ModelCacheSet)))
                for row in sets:
                    previous = (row.state, row.verified_bytes, row.protected, row.protected_reasons)
                    manifest = ArtifactSetManifest.from_document(row.manifest)
                    verified = self._verified_bytes(session, row.artifact_set_sha256)
                    row.verified_bytes = verified
                    if row.state not in {"downloading", "verifying"}:
                        row.state = (
                            "cached"
                            if self._manifest_coverage_complete(manifest)
                            else "needs-repair"
                        )
                    self._refresh_protection(session, row)
                    if previous != (row.state, row.verified_bytes, row.protected, row.protected_reasons):
                        row.updated_at = self._clock()
            return self.storage_summary().document()

    def _refresh_protection(self, session: Session, row: ModelCacheSet) -> None:
        # Protection is a projection of durable references. Recompute it from
        # those references so removal of the last reference makes a set
        # evictable without retaining an old flag.
        reasons: set[str] = set()
        if row.model_content_sha256 is not None:
            cache_model_digests = self._cache_model_content_digests(row)
            installations = session.scalars(
                select(RecipeInstallation).where(
                    RecipeInstallation.state.in_(["planned", "installing", "installed", "partial"]),
                )
            )
            if any(
                self._recipe_references_cache(
                    session, installation.recipe_revision_id, cache_model_digests
                )
                for installation in installations
            ):
                reasons.add("recipe-installation")
            running_installation_ids = session.scalars(
                select(RecipeRun.installation_id).where(
                    RecipeRun.state.in_(["planned", "starting", "running"]),
                )
            )
            if any(
                self._recipe_references_cache(
                    session, installation_id, cache_model_digests
                )
                for installation_id in running_installation_ids
            ):
                reasons.add("running-model")
        for profile in session.scalars(select(FleetProfile)):
            if _contains_digest(
                profile.assignments,
                row.model_content_sha256,
                row.recipe_revision_sha256,
            ) or self._profile_references_cache(session, profile, row):
                reasons.add("saved-profile")
        row.protected = bool(reasons)
        row.protected_reasons = sorted(reasons)

    @staticmethod
    def _cache_model_content_digests(row: ModelCacheSet) -> set[str]:
        try:
            manifest = ArtifactSetManifest.from_document(row.manifest)
        except ModelCacheError:
            return {row.model_content_sha256} if row.model_content_sha256 is not None else set()
        return set(manifest.model_content_digests) or (
            {row.model_content_sha256} if row.model_content_sha256 is not None else set()
        )

    def _recipe_references_cache(
        self,
        session: Session,
        revision_id: str,
        cache_model_digests: set[str],
    ) -> bool:
        revision = session.get(CatalogDocumentRevision, revision_id)
        if revision is None or revision.kind != "recipe" or revision.state != "active":
            return False
        try:
            recipe = read_catalog_document(revision)
            if not isinstance(recipe, RecipeDefinition):
                raise TypeError("catalog revision is not a recipe")
            direct_model_digests = set(_recipe_model_content_digests(recipe))
            if cache_model_digests.intersection(direct_model_digests):
                return True
            model_rows: dict[str, CatalogDocumentRevision] = {}
            for digest in direct_model_digests:
                self._collect_model_definitions(session, digest, model_rows)
        except ModelCacheError:
            return False
        return bool(cache_model_digests.intersection(model_rows))

    def _profile_references_cache(
        self,
        session: Session,
        profile: FleetProfile,
        row: ModelCacheSet,
    ) -> bool:
        """Resolve profile recipe IDs before deciding a cache set is evictable."""
        assignments = profile.assignments
        if not isinstance(assignments, list):
            return False
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                continue
            revision_id = assignment.get("recipe_revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                continue
            revision = session.get(CatalogDocumentRevision, revision_id)
            if revision is None or revision.kind != "recipe" or revision.state != "active":
                continue
            if (
                row.recipe_revision_sha256 is not None
                and revision.content_digest == row.recipe_revision_sha256
            ):
                return True
            if row.model_content_sha256 is None:
                continue
            if self._recipe_references_cache(
                session, revision_id, self._cache_model_content_digests(row)
            ):
                return True
        return False

    def _update_flags(
        self, session: Session, row: ModelCacheSet, manifest: ArtifactSetManifest
    ) -> tuple[bool, bool]:
        model_update = self._model_update_candidate(session, manifest) is not None
        recipe_update = False
        if row.recipe_revision_sha256 is not None:
            latest_recipe = self._latest_recipe_digest(
                session, row.recipe_revision_sha256
            )
            recipe_update = latest_recipe is not None and latest_recipe != row.recipe_revision_sha256
        return model_update, recipe_update

    @staticmethod
    def _model_update_candidate(
        session: Session, manifest: ArtifactSetManifest
    ) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision] | None:
        current, candidates = ModelCacheService._model_update_candidates(session, manifest)
        if current is None or len(candidates) != 1:
            return None
        return current, candidates[0]

    @staticmethod
    def _model_update_candidates(
        session: Session, manifest: ArtifactSetManifest
    ) -> tuple[CatalogDocumentRevision | None, list[CatalogDocumentRevision]]:
        ref = manifest.model_definition_ref
        if ref is None:
            return None, []
        current_digest = ref.content_sha256
        current = None
        current = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest == current_digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if current is None:
            current = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.publisher == ref.publisher,
                    CatalogDocumentRevision.slug == ref.slug,
                    CatalogDocumentRevision.state == "active",
                ).order_by(CatalogDocumentRevision.revision_number.asc())
            )
        if current is None:
            return None, []
        current_document = read_catalog_document(current)
        if not isinstance(current_document, ModelDefinition):
            return None, []
        current_signature = _model_lineage_signature(current_document)
        candidates: list[CatalogDocumentRevision] = []
        for candidate in session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ):
            if candidate.content_digest == current.content_digest:
                continue
            candidate_document = read_catalog_document(candidate)
            if not isinstance(candidate_document, ModelDefinition):
                continue
            same_lineage = _model_lineage_signature(candidate_document) == current_signature
            if not same_lineage and not _supersedes_revision(candidate, current):
                continue
            if not _same_model_artifact_identity(candidate, manifest) and (
                candidate.revision_number > current.revision_number
                or _datetime(candidate.created_at) > _datetime(current.created_at)
                or _supersedes_revision(candidate, current)
            ):
                candidates.append(candidate)
        # Multiple incomparable successors are deliberately exposed as
        # ambiguous; choosing one by wall-clock order would hide a catalog
        # lineage decision from operators.
        explicit = [item for item in candidates if _supersedes_revision(item, current)]
        if explicit:
            return current, explicit
        return current, candidates

    @staticmethod
    def _latest_recipe_digest(session: Session, digest: str) -> str | None:
        current = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.content_digest == digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if current is None:
            return None
        latest = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.document_id == current.document_id,
                CatalogDocumentRevision.state == "active",
            )
            .order_by(CatalogDocumentRevision.revision_number.desc())
        )
        return None if latest is None else latest.content_digest

    def _check_upstream_revision(self, repository: str, revision: str) -> dict[str, object]:
        """Inspect provider metadata only; catalog import owns accepting new pins."""
        result: dict[str, object] = {
            "repository": repository,
            "pinned_revision": revision,
            "latest_revision": None,
            "status": "check-failed",
            "checked_at": _iso(self._clock()),
            "error_code": None,
        }
        own_client = self._http is None
        client = self._http or httpx.Client(timeout=20, follow_redirects=False)
        try:
            response = self._open_http_response(
                client, f"https://huggingface.co/api/models/{repository}/revision/main", {}
            )
            try:
                response.read()
                document = response.json()
            finally:
                response.close()
            latest = document.get("sha") if isinstance(document, dict) else None
            if not isinstance(latest, str) or not re.fullmatch(r"[0-9a-f]{40,64}", latest):
                raise ModelCacheResolutionError(
                    "model_cache.upstream_revision_invalid",
                    "provider metadata did not identify an immutable revision",
                )
            result.update(
                latest_revision=latest,
                status="current" if latest == revision else "update-available",
            )
        except (ModelCacheError, httpx.HTTPError, ValueError, OSError) as error:
            # Provider failures must not hide accepted catalog updates or
            # expose signed URLs/credentials in the public response.
            result["error_code"] = getattr(error, "code", "model_cache.upstream_check_failed")
        finally:
            if own_client:
                client.close()
        return result

    def _check_upstream_revisions(
        self, identities: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, object]]:
        """Bound metadata concurrency and total response latency across the page."""
        ordered = list(dict.fromkeys(identities))
        results: dict[tuple[str, str], dict[str, object]] = {}
        pending: dict[Future, tuple[str, str]] = {}
        deadline = time.monotonic() + _UPSTREAM_CHECK_SECONDS
        remaining = iter(ordered)
        exhausted = False
        while time.monotonic() < deadline:
            while not exhausted and len(pending) < _UPSTREAM_CHECK_WORKERS:
                if not self._upstream_slots.acquire(blocking=False):
                    break
                key = next(remaining, None)
                if key is None:
                    self._upstream_slots.release()
                    exhausted = True
                    break
                try:
                    future = self._upstream_executor.submit(self._check_upstream_revision, *key)
                except RuntimeError:
                    self._upstream_slots.release()
                    break
                future.add_done_callback(lambda _: self._upstream_slots.release())
                pending[future] = key
            if not pending:
                break
            done, _ = wait(pending, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                key = pending.pop(future)
                try:
                    results[key] = future.result()
                except Exception:  # noqa: BLE001 - diagnostics must not hide local catalog results
                    # A diagnostic/provider bug cannot hide the catalog page.
                    results[key] = {
                        "repository": key[0], "pinned_revision": key[1],
                        "latest_revision": None, "status": "check-failed",
                        "checked_at": _iso(self._clock()),
                        "error_code": "model_cache.upstream_check_failed",
                    }
        for future in pending:
            future.cancel()
        for repository, revision in ordered:
            results.setdefault((repository, revision), {
                "repository": repository,
                "pinned_revision": revision,
                "latest_revision": None,
                "status": "check-failed",
                "checked_at": _iso(self._clock()),
                "error_code": "model_cache.upstream_check_budget_exhausted",
            })
        return results

    def discover_updates(
        self,
        *,
        artifact_set_sha256: str | None = None,
        limit: int = 100,
        check_upstream: bool = False,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a bounded, deterministic update page.

        Update discovery is metadata only: it never changes the immutable
        model pin or the active profile/run reference.  The optional exact
        set filter is used by the CLI and keeps a large NAS inventory from
        becoming an unbounded response.
        """
        if not 1 <= limit <= 100:
            raise ValueError("cache update limit is invalid")
        requested_set = _optional_digest(artifact_set_sha256)
        self.reconcile_storage()
        with self._session() as session:
            query = select(ModelCacheSet).order_by(
                ModelCacheSet.updated_at.desc(),
                ModelCacheSet.artifact_set_sha256.desc(),
            )
            if requested_set is not None:
                query = query.where(ModelCacheSet.artifact_set_sha256 == requested_set)
            rows = list(session.scalars(query))
            total = len(rows)
            start = 0
            if boundary is not None:
                boundary_time = _parse_iso(boundary[0])
                for index, row in enumerate(rows):
                    if (
                        _datetime(row.updated_at) == boundary_time
                        and row.artifact_set_sha256 == boundary[1]
                    ):
                        start = index + 1
                        break
                else:
                    raise ModelCacheConflict(
                        "model_cache.cursor_invalid",
                        "cache update cursor boundary is stale",
                    )
            page = rows[start : start + limit]
            result = []
            upstream_sources: dict[str, list[tuple[str, str]]] = {}
            for row in page:
                manifest = ArtifactSetManifest.from_document(row.manifest)
                model_update, recipe_update = self._update_flags(session, row, manifest)
                latest_model = None
                model_update_from = None
                model_update_to = None
                model_update_candidates: list[dict[str, object]] = []
                model_update_ambiguous = False
                latest_recipe = None
                current_model, candidates = self._model_update_candidates(session, manifest)
                if current_model is not None and len(candidates) == 1:
                    latest = candidates[0]
                    latest_model = latest.content_digest
                    model_update_from = _revision_identity(current_model)
                    model_update_to = _revision_identity(latest)
                elif current_model is not None and candidates:
                    model_update_ambiguous = True
                    model_update_candidates = [
                        identity
                        for candidate in candidates
                        if (identity := _revision_identity(candidate)) is not None
                    ]
                if recipe_update and row.recipe_revision_sha256 is not None:
                    latest_recipe = self._latest_recipe_digest(
                        session, row.recipe_revision_sha256
                    )
                sources: list[tuple[str, str]] = []
                if check_upstream:
                    for spec in manifest.artifacts:
                        if spec.kind != "huggingface.file" or spec.revision is None:
                            continue
                        repository = "/".join(urlsplit(spec.source).path.strip("/").split("/")[:2])
                        key = (repository, spec.revision)
                        if key not in sources:
                            sources.append(key)
                upstream_sources[row.artifact_set_sha256] = sources
                result.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "artifact_set_sha256": row.artifact_set_sha256,
                        "model_content_sha256": row.model_content_sha256,
                        "latest_model_content_sha256": latest_model,
                        "model_update_from": model_update_from,
                        "model_update_to": model_update_to,
                        "model_update_ambiguous": model_update_ambiguous,
                        "model_update_candidates": model_update_candidates,
                        "recipe_revision_sha256": row.recipe_revision_sha256,
                        "latest_recipe_revision_sha256": latest_recipe,
                        "upstream_revisions": [],
                        "model_update_available": model_update,
                        "recipe_update_available": recipe_update,
                        "updated_at": _iso(row.updated_at),
                    }
                )
            next_boundary = None
            if start + limit < total and page:
                last = page[-1]
                next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
        # All catalog values above are detached JSON snapshots. Provider I/O
        # must not retain a DB connection or transaction while waiting.
        if check_upstream:
            upstream_checks = self._check_upstream_revisions([
                key for sources in upstream_sources.values() for key in sources
            ])
            for entry in result:
                entry["upstream_revisions"] = [
                    upstream_checks[key] for key in upstream_sources[entry["artifact_set_sha256"]]
                ]
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "updates": tuple(result),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def storage_summary(self) -> StorageSummary:
        usage = shutil.disk_usage(self._root)
        object_bytes: dict[str, int] = {}
        objects = self._root / "objects"
        for path in objects.glob("*/*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and len(path.name) == _DIGEST_LENGTH
                and path.name == path.name.lower()
                and _is_hex(path.name)
                and path.parent.name == path.name[:2]
            ):
                try:
                    object_bytes[path.name] = path.stat().st_size
                except OSError:
                    continue
        unique_used = sum(object_bytes.values())
        with self._session(write=True) as session:
            sets = list(session.scalars(select(ModelCacheSet)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            operations = list(
                session.scalars(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.state.in_(["queued", "running", "partial"])
                    )
                )
            )
            for row in sets:
                self._refresh_protection(session, row)
            protected_sets = {row.artifact_set_sha256 for row in sets if row.protected}
            protected_artifacts = {
                item.artifact_sha256
                for item in memberships
                if item.artifact_set_sha256 in protected_sets
            }
            in_flight_artifacts: set[str] = set()
            for operation in operations:
                if operation.kind not in {"download", "repair"}:
                    continue
                payload = _validated_operation_payload(operation)
                manifest = ArtifactSetManifest.from_document(payload["manifest"])
                in_flight_artifacts.update(item.sha256 for item in manifest.artifacts)
            artifact_rows = {
                row.sha256: row for row in session.scalars(select(ModelCacheArtifact))
            }
            protected_bytes = sum(
                object_bytes.get(digest, 0)
                for digest in protected_artifacts
            )
            # Any on-disk object that is not protected can be reclaimed.  This
            # includes orphaned files left by an interrupted atomic publish;
            # reporting physical bytes keeps capacity decisions honest.
            reclaimable_bytes = sum(
                size
                for digest, size in object_bytes.items()
                if digest not in protected_artifacts
            )
            partial_bytes: dict[str, int] = {}
            partial_root = self._root / "partials"
            for partial in partial_root.glob("*/*.part"):
                if (
                    partial.is_file()
                    and not partial.is_symlink()
                    and len(partial.stem) == _DIGEST_LENGTH
                    and _is_hex(partial.stem)
                ):
                    try:
                        partial_bytes[partial.stem] = max(
                            partial_bytes.get(partial.stem, 0), partial.stat().st_size
                        )
                    except OSError:
                        continue
            in_flight_bytes = sum(
                max(
                    0,
                    artifact_rows[digest].expected_bytes
                    - max(
                        object_bytes.get(digest, 0),
                        partial_bytes.get(digest, 0),
                        int(artifact_rows[digest].actual_bytes),
                    ),
                )
                for digest in in_flight_artifacts
                if digest in artifact_rows
            )
        available = max(0, usage.free - self._reserve_bytes)
        return StorageSummary(
            total_bytes=usage.total,
            free_bytes=usage.free,
            reserve_bytes=self._reserve_bytes,
            available_bytes=available,
            unique_used_bytes=unique_used,
            in_flight_bytes=in_flight_bytes,
            protected_bytes=protected_bytes,
            reclaimable_bytes=reclaimable_bytes,
        )

    def eviction_preview(
        self,
        *,
        target_bytes: int,
    ) -> dict[str, object]:
        if not isinstance(target_bytes, int) or isinstance(target_bytes, bool) or target_bytes <= 0:
            raise ValueError("eviction target must be positive")
        self.reconcile_storage()
        before = self.storage_summary()
        with self._session(write=True) as session:
            rows = list(session.scalars(select(ModelCacheSet).order_by(ModelCacheSet.last_accessed_at)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            by_set: dict[str, list[ModelCacheSetArtifact]] = {}
            for membership in memberships:
                by_set.setdefault(membership.artifact_set_sha256, []).append(membership)
            object_paths = {
                artifact.sha256: self._object_path(artifact.sha256)
                for artifact in session.scalars(select(ModelCacheArtifact))
            }
            selected: list[dict[str, object]] = []
            protected_entries: list[dict[str, object]] = []
            selected_sets: set[str] = set()
            for row in rows:
                self._refresh_protection(session, row)
                entry = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": row.artifact_set_sha256,
                    "reclaimable_bytes": 0,
                    "protected": bool(row.protected),
                    "protected_reasons": list(row.protected_reasons or ()),
                    "last_accessed_at": _iso(row.last_accessed_at) or "",
                }
                if row.protected:
                    protected_entries.append(entry)
                elif row.state != "cached":
                    continue
                else:
                    selected.append(entry)
            # A shared object is reclaimable only when every referencing set
            # is in the selected removal plan.  Build the plan incrementally
            # in LRU order and report actual object bytes, not declared sizes.
            candidates = [entry for entry in selected]
            chosen: list[dict[str, object]] = []
            chosen_objects: set[str] = set()
            previous_bytes = 0
            for entry in candidates:
                selected_sets.add(str(entry["artifact_set_sha256"]))
                chosen.append(entry)
                for membership in by_set.get(str(entry["artifact_set_sha256"]), ()):
                    digest = membership.artifact_sha256
                    references = {
                        item.artifact_set_sha256
                        for item in memberships
                        if item.artifact_sha256 == digest
                    }
                    if references <= selected_sets:
                        chosen_objects.add(digest)
                cumulative_bytes = sum(
                    object_paths[digest].stat().st_size
                    for digest in chosen_objects
                    if digest in object_paths and object_paths[digest].is_file()
                )
                entry["reclaimable_bytes"] = max(0, cumulative_bytes - previous_bytes)
                previous_bytes = cumulative_bytes
                if cumulative_bytes >= target_bytes:
                    break
            selected_bytes = sum(
                object_paths[digest].stat().st_size
                for digest in chosen_objects
                if digest in object_paths and object_paths[digest].is_file()
            )
            # Include protected rows in the review so the operator sees why
            # the target cannot be met; apply never permits deleting them.
            blockers: list[str] = []
            if selected_bytes < target_bytes:
                if protected_entries:
                    blockers.append("protected entries require separate reference removal")
                else:
                    blockers.append("target-exceeds-reclaimable-bytes")
            plan = {
                "schema_version": SCHEMA_VERSION,
                "kind": "evict",
                "target_bytes": target_bytes,
                "selected": [entry["artifact_set_sha256"] for entry in chosen],
                "selected_objects": sorted(chosen_objects),
            }
            plan_digest = _sha256_json(plan)
            after = StorageSummary(
                total_bytes=before.total_bytes,
                free_bytes=before.free_bytes + selected_bytes,
                reserve_bytes=before.reserve_bytes,
                available_bytes=before.available_bytes + selected_bytes,
                unique_used_bytes=max(0, before.unique_used_bytes - selected_bytes),
                in_flight_bytes=before.in_flight_bytes,
                protected_bytes=before.protected_bytes,
                reclaimable_bytes=max(0, before.reclaimable_bytes - selected_bytes),
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "plan_digest": plan_digest,
                "target_bytes": target_bytes,
                "selected": chosen,
                "protected_entries": protected_entries,
                "reclaimable_bytes": before.reclaimable_bytes,
                "selected_bytes": selected_bytes,
                "storage_before": before.document(),
                "storage_after": after.document(),
                "blockers": blockers,
                "_selected_objects": sorted(chosen_objects),
            }

    def evict(
        self,
        *,
        actor: str,
        request_key: str,
        plan_digest: str,
        target_bytes: int,
    ) -> CacheOperationView:
        request_key = _request_key(request_key)
        requested_plan = _optional_digest(plan_digest)
        assert requested_plan is not None
        preview = self.eviction_preview(target_bytes=target_bytes)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "eviction preview is stale")
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.eviction_blocked",
                "; ".join(str(item) for item in preview["blockers"]),
            )
        selected = [str(item["artifact_set_sha256"]) for item in preview["selected"]]
        if any(bool(item["protected"]) for item in preview["selected"]):
            raise ModelCacheConflict(
                "model_cache.protected_reference",
                "protected cache content must be unreferenced before removal",
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "target_bytes": target_bytes,
            "selected": selected,
            "selected_objects": list(
                preview.get("_selected_objects", ())
            ),
            "before_unique_used_bytes": self.storage_summary().unique_used_bytes,
        }
        payload = _write_operation_payload("evict", payload)
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                _validated_operation_payload(existing)
                if existing.kind != "evict" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="evict",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=None,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress=cache_progress({
                        "schema_version": SCHEMA_VERSION,
                        "phase": "queued",
                        "completed_artifacts": 0,
                        "total_artifacts": len(selected),
                        "downloaded_bytes": 0,
                        "expected_bytes": int(preview["selected_bytes"]),
                        "current_artifact_key": None,
                    }, previous=None, now=self._clock()),
                    actor=actor,
                    created_at=self._clock(),
                    updated_at=self._clock(),
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _run_eviction(self, operation_id: str) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            payload = _validated_operation_payload(operation)
            selected = tuple(payload["selected"])
            before_unique = payload["before_unique_used_bytes"]
        self._set_operation_state(operation_id, "running")
        try:
            with self._session(write=True) as session:
                for index, digest in enumerate(selected, start=1):
                    row = session.get(ModelCacheSet, digest)
                    if row is None:
                        continue
                    self._refresh_protection(session, row)
                    if row.protected:
                        raise ModelCacheConflict(
                            "model_cache.protected_reference",
                            "protected cache content changed before eviction",
                        )
                    session.query(ModelCacheSetArtifact).filter(
                        ModelCacheSetArtifact.artifact_set_sha256 == digest
                    ).delete(synchronize_session=False)
                    session.delete(row)
                    operation = session.get(ModelCacheOperation, operation_id)
                    if operation is not None:
                        operation_progress = _validated_operation_progress(operation)
                        operation.progress = cache_progress({
                            "schema_version": SCHEMA_VERSION,
                            "phase": "reclaiming",
                            "completed_artifacts": index,
                            "total_artifacts": len(selected),
                            "downloaded_bytes": 0,
                            "expected_bytes": operation_progress.expected_bytes or 0,
                            "current_artifact_key": None,
                        },
                            previous=operation_progress.model_dump(mode="json"),
                            now=self._clock(),
                        )
                session.flush()
                referenced = {
                    item.artifact_sha256
                    for item in session.scalars(select(ModelCacheSetArtifact))
                }
                for artifact in list(session.scalars(select(ModelCacheArtifact))):
                    if artifact.sha256 in referenced:
                        continue
                    path = self._object_path(artifact.sha256)
                    if path.exists() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    session.delete(artifact)
            self._set_operation_state(
                operation_id,
                "succeeded",
                result={
                    "schema_version": SCHEMA_VERSION,
                    "removed_entries": list(selected),
                    "reclaimed_bytes": max(
                        0,
                        before_unique - self.storage_summary().unique_used_bytes,
                    ),
                },
            )
        except (ModelCacheError, OSError, ValueError) as error:
            now = self._clock()
            with self._session(write=True) as session:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is not None:
                    operation.state = "failed"
                    operation.last_error = redact_text(
                        error.detail if isinstance(error, ModelCacheError) else str(error)
                    )[:512]
                    payload = _validated_operation_payload(operation)
                    payload.pop("claim", None)
                    payload.pop("result", None)
                    payload["failure"] = _cache_failure(
                        error.code if isinstance(error, ModelCacheError) else "model_cache.eviction_failed",
                        operation.last_error, retryable=False, recovery="inspect",
                    )
                    operation.payload = _write_operation_payload("evict", payload)
                    operation.progress = cache_phase(
                        _validated_operation_progress(operation).model_dump(mode="json"),
                        "failed",
                        now,
                    )
                    operation.updated_at = now
                    operation.completed_at = now

    def _refresh_entry_state(self, session: Session, set_digest: str) -> None:
        row = session.get(ModelCacheSet, set_digest)
        if row is None:
            return
        manifest = ArtifactSetManifest.from_document(row.manifest)
        row.verified_bytes = self._verified_bytes(session, set_digest)
        if row.state not in {"downloading", "verifying"}:
            row.state = (
                "cached"
                if self._manifest_coverage_complete(manifest)
                else "needs-repair"
            )

    def _object_key(self, digest: str) -> str:
        return f"objects/{digest[:2]}/{digest}"

    def _object_path(self, digest: str) -> Path:
        return self._root / "objects" / digest[:2] / digest

    def _partial_path(self, set_digest: str, digest: str) -> Path:
        return self._root / "partials" / set_digest / f"{digest}.part"



def _request_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ModelCacheConflict("model_cache.request_key_invalid", "request key is invalid") from error


def _is_private_host(value: str) -> bool:
    normalized = value.lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _is_hf_authority(value: str | None) -> bool:
    if not value:
        return False
    host = value.lower().rstrip(".")
    return (
        host == _HF_CANONICAL_HOST or host.endswith((".huggingface.co", ".hf.co"))
    )


def _is_hf_canonical_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") == _HF_CANONICAL_HOST
        and parsed.port is None
    )


def _is_allowed_huggingface_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and _is_hf_authority(parsed.hostname)
        and not _is_private_host(parsed.hostname)
    )


def _valid_relative_path(value: str) -> bool:
    return bool(
        value
        and len(value) <= 512
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _contains_digest(value: object, model_digest: str | None, recipe_digest: str | None) -> bool:
    if model_digest is None and recipe_digest is None:
        return False
    if isinstance(value, Mapping):
        return any(_contains_digest(child, model_digest, recipe_digest) for child in value.values()) or (
            (model_digest is not None and value.get("model_content_sha256") == model_digest)
            or (recipe_digest is not None and value.get("recipe_revision_sha256") == recipe_digest)
        )
    if isinstance(value, list):
        return any(_contains_digest(child, model_digest, recipe_digest) for child in value)
    return False


def _eviction_plan_objects(preview: Mapping[str, object], root: Path) -> tuple[str, ...]:
    # The preview exposes selected bytes and entries; the apply operation
    # revalidates membership and computes object reachability again.  This
    # helper intentionally returns no filesystem-derived authority.
    del root
    values = preview.get("selected_objects")
    if isinstance(values, list):
        return tuple(str(value) for value in values)
    return ()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_POLICY",
    "ArtifactSetManifest",
    "ArtifactSpec",
    "CacheOperationView",
    "ModelCacheConflict",
    "ModelCacheError",
    "ModelCacheNotFound",
    "ModelCacheResolutionError",
    "ModelCacheService",
    "ModelCacheStorageError",
    "StorageSummary",
]
