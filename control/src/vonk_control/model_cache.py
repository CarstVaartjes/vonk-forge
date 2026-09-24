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
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BufferedReader
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import OperationMemberProgress, canonical_message
from vonk_forge_contracts import ModelDefinition, RecipeDefinition
from vonk_forge_contracts.model import ModelReference

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactKind,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    check_removal_fence_nowait,
    clear_removal,
    lock_removal_fences,
    reference_gate_is_open_nowait,
    removal_fences_match,
    reserve_removal,
    retryable_artifact_database_error,
)
from .artifact_reference_scan import (
    model_set_objects,
    model_set_reference_findings,
    model_set_reference_reasons,
    require_model_sets_open,
)
from .bounded_json import mapping, require_integer, require_mapping, require_sequence
from .cache_removal_review import (
    AssetAvailability,
    AssetDisposition,
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)
from .cached_file_verification import verified_files
from .catalog_queries import active_head_revision
from .catalog_revision_contract import read_catalog_document
from .logging import log_event, redact_text
from .model_cache_contract import (
    UUID_PATTERN,
    CacheManifest,
    CacheManifestArtifact,
    ModelCacheCancellation,
    ModelCacheCancellationRequest,
    ModelCacheDownloadPayload,
    ModelCacheObjectReceipt,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
    ModelCacheOperationResponse,
    ModelCacheOperationResult,
    ModelCacheOperatorAction,
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    ModelCacheRepairCheckpoint,
    ModelCacheRepairPayload,
    ModelCacheTransfer,
    parse_model_cache_payload,
    parse_model_cache_result,
)
from .model_cache_progress import cache_phase, cache_progress
from .model_cache_ranges import cleanup_ranges, download_ranges, range_partial_bytes
from .models import (
    ArtifactLifecycleGate,
    CatalogDocumentRevision,
    FleetProfile,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
    RuntimeImageAuthorization,
)
from .operation_contract import AvailabilityOperationFailure
from .runtime_init import RuntimeSecretError, read_runtime_secret
from .strict_json import serialize_json_value

SCHEMA_VERSION = 2
SOURCE_POLICY = "nas-first"
_DIGEST_LENGTH = 64
_DIGEST_PATTERN = r"[0-9a-f]{64}"
_MAX_ARTIFACTS = 1024
_MAX_MANIFEST_BYTES = 1_048_576
_CHUNK_BYTES = 1024 * 1024
_PARALLEL_RANGE_MIN_BYTES = 64 * 1024 * 1024
_PARALLEL_RANGE_WORKERS = 4
_MAX_HTTP_REDIRECTS = 3
_MAX_OPERATION_ATTEMPTS = 3
_MAX_OPERATOR_RETRIES = 3
_DEFAULT_MAX_PARALLEL_DOWNLOADS = 8
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
_LOGGER = logging.getLogger(__name__)
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
        # Coerce before bounding: a caller that passes a sequence would
        # otherwise leave a non-string in place, and the ``str(error)``
        # fallback every failure path uses would then read ``[]``.
        self.detail = str(detail)[:512]
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


class _ArtifactWriterBusy(ModelCacheError):
    """A dependency wait, not a failed transfer or consumed retry."""

    def __init__(self, digest: str) -> None:
        super().__init__(
            "model_cache.object_busy",
            f"waiting for the managed-cache writer of object {digest}; resumes after that writer releases its lock",
            retry_after_seconds=_RETRY_BASE_SECONDS,
            recovery="resume",
        )


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
        values.append(
            max(0, reset - int(now.timestamp()))
            if reset > 1_000_000_000
            else max(0, reset)
        )
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
class ModelCacheRemovalScope:
    """Exact SQL membership and unshared bytes for one accepted removal."""

    selected_sets: tuple[str, ...]
    memberships: tuple[tuple[str, tuple[str, ...]], ...]
    selected_objects: tuple[str, ...]
    delete_objects: tuple[str, ...]
    shared_memberships: tuple[tuple[str, str, str], ...]


def _scope_matches_review(
    scope: ModelCacheRemovalScope, review: CacheRemovalReview
) -> bool:
    """Compare the reviewed artifact identities with the current SQL scope."""

    sets = {asset.sha256 for asset in review.assets if asset.kind == "model-set"}
    objects = {asset.sha256 for asset in review.assets if asset.kind == "model-object"}
    delete_objects = {
        asset.sha256
        for asset in review.assets
        if asset.kind == "model-object" and asset.disposition == "remove"
    }
    shared_memberships = {
        (item.asset_sha256, item.owner_id, item.state)
        for item in review.references
        if item.owner_kind == "model-cache-set-membership"
    }
    return (
        sets == set(scope.selected_sets)
        and objects == set(scope.selected_objects)
        and delete_objects == set(scope.delete_objects)
        and shared_memberships == set(scope.shared_memberships)
    )


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
    def from_document(cls, value: object) -> ArtifactSetManifest:
        try:
            document = require_mapping(value, "cache manifest must be a JSON object")
            _reject_non_json_containers(document)
            wire = CacheManifest.model_validate_json(canonical_message(document))
        except (TypeError, ValueError, ValidationError) as error:
            code = (
                "model_cache.schema_unsupported"
                if isinstance(error, ValidationError)
                and any(
                    issue.get("loc") == ("schema_version",) for issue in error.errors()
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


def _model_removal_intent_digest(
    payload: ModelCacheRemovalPayload, *, actor: str, request_key: str
) -> str:
    """Bind immutable effects and authority, excluding advancing checkpoints."""
    return _sha256_json(
        {
            "action": "remove-model",
            "actor": actor,
            "request_key": request_key,
            "selector": payload.selector,
            "model_content_sha256": payload.model_content_sha256,
            "removal_fence": payload.removal_fence,
            "selected": payload.selected,
            "selected_objects": payload.selected_objects,
            "delete_objects": payload.delete_objects,
        }
    )


def _validated_operation_payload(
    operation: ModelCacheOperation,
) -> dict[str, object]:
    """Read and normalize one persisted operation envelope."""

    try:
        parsed = parse_model_cache_payload(operation.kind, operation.payload)
        if isinstance(
            parsed, ModelCacheRemovalPayload
        ) and operation.plan_digest != _model_removal_intent_digest(
            parsed, actor=operation.actor, request_key=operation.request_key
        ):
            raise ModelCacheStorageError(
                "model_cache.removal_plan_changed",
                "persisted model removal intent no longer matches its accepted plan",
            )
        if isinstance(parsed, (ModelCacheDownloadPayload, ModelCacheRepairPayload)):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return dict(
            require_mapping(serialize_json_value(parsed), "cache operation payload")
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageError(
            "model_cache.payload_invalid",
            "persisted cache operation payload is invalid",
        ) from error


def _removal_document(payload: Mapping[str, object]) -> ModelCacheRemovalPayload:
    parsed = parse_model_cache_payload("remove", payload)
    if not isinstance(parsed, ModelCacheRemovalPayload):
        raise ModelCacheStorageError(
            "model_cache.payload_invalid", "model removal payload is invalid"
        )
    return parsed


def _operation_cancellation(
    operation: ModelCacheOperation,
) -> dict[str, object] | None:
    raw = _validated_operation_payload(operation).get("cancellation")
    if raw is None:
        return None
    try:
        return ModelCacheCancellation.model_validate(raw).model_dump(mode="json")
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageError(
            "model_cache.payload_invalid",
            "persisted model cancellation intent is invalid",
        ) from error


def _validated_operation_progress(
    operation: ModelCacheOperation,
) -> ModelCacheOperationProgress:
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


def _write_operation_payload(
    kind: str, value: Mapping[str, object]
) -> dict[str, object]:
    """Validate and normalize a newly assembled operation envelope."""

    try:
        parsed = parse_model_cache_payload(kind, value)
        if isinstance(parsed, (ModelCacheDownloadPayload, ModelCacheRepairPayload)):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return dict(
            require_mapping(serialize_json_value(parsed), "cache operation payload")
        )
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
    model_content_sha256: str | None
    artifact_set_sha256: str | None
    plan_digest: str | None
    review_digest: str | None
    progress: Mapping[str, object]
    result: ModelCacheOperationResult | None
    last_error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    retryable: bool = False
    failure: Mapping[str, object] | None = None
    cancellation: Mapping[str, object] | None = None


def _cache_failure(
    code: str,
    detail: str,
    *,
    retryable: bool,
    recovery: str,
    retry_time: str | None = None,
    retry_after_seconds: int | None = None,
    required_bytes: int | None = None,
    free_bytes: int | None = None,
    shortfall_bytes: int | None = None,
    artifact_key: str | None = None,
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
        "access_required": [
            "open_model_access",
            "configure_hf_token",
            "check_access_and_resume",
        ],
        "access_denied": ["open_model_access", "check_access_and_resume"],
        "credentials_invalid": ["configure_hf_token", "check_access_and_resume"],
        "resume": ["resume"],
        "download_again": ["download_again"],
        "retry": ["retry"],
        "capacity": ["free_space", "resume"],
        "check_access_and_resume": ["check_access_and_resume"],
        "inspect": ["inspect"],
    }
    return AvailabilityOperationFailure.model_validate(
        {
            "code": semantic_codes.get(code, code),
            "detail": redact_text(detail)[:512],
            "retryable": retryable,
            "recovery_actions": recovery_actions[recovery],
            "retry_time": retry_time,
            "retry_after_seconds": retry_after_seconds,
            "required_bytes": required_bytes,
            "free_bytes": free_bytes,
            "shortfall_bytes": shortfall_bytes,
            "artifact_key": artifact_key,
            "log_excerpt": redact_text(detail)[:1024],
        }
    ).model_dump(mode="json")


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
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-"
            for character in value.key
        )
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
        or (value.expected_bytes == 0 and value.sha256 != _EMPTY_SHA256)
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
    if any(
        not isinstance(item, str) or not _is_hex(item)
        for item in value.model_content_digests
    ):
        raise ModelCacheResolutionError(
            "model_cache.model_content_digests_invalid",
            "cache model dependency pins are invalid",
        )
    encoded = json.dumps(
        value.document(), sort_keys=True, separators=(",", ":")
    ).encode()
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
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", revision
        ):
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
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            value,
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


def _recipe_definition(
    document: Mapping[str, object] | RecipeDefinition,
) -> RecipeDefinition:
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
        if (
            not isinstance(digest, str)
            or not _is_hex(digest)
            or len(digest) != _DIGEST_LENGTH
        ):
            raise ModelCacheResolutionError(
                "model_cache.model_pin_invalid", "canonical recipe model pin is invalid"
            )
        if digest not in result:
            result.append(digest)
    if len(result) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionError(
            "model_cache.dependency_count", "recipe model set is too large"
        )
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
            "model_cache.model_definition_invalid",
            "canonical model definition is invalid",
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
            str(item["id"]): (
                str(item["path"]),
                str(item["sha256"]),
                require_integer(item["download_bytes"], "download bytes"),
            )
            for item in _canonical_model_artifacts(row)
        }
    except (KeyError, TypeError, ValueError, ModelCacheResolutionError):
        return False
    selected = {
        item.artifact_id: (item.path, item.sha256, item.expected_bytes)
        for item in manifest.artifacts
    }
    return bool(selected) and all(
        current.get(key) == identity for key, identity in selected.items()
    )


def _model_lineage_signature(
    document: Mapping[str, object] | ModelDefinition,
) -> tuple[object, object, object]:
    """Return the logical model and representation identity from ModelDefinition."""

    model = (
        document
        if isinstance(document, ModelDefinition)
        else ModelDefinition.model_validate(document)
    )
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
    """Resolve, download, verify, repair and remove NAS model artifacts."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        root: Path,
        *,
        reserve_bytes: int = 10 * 1024**3,
        max_parallel_downloads: int = _DEFAULT_MAX_PARALLEL_DOWNLOADS,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx.Client | None = None,
        fixture_sources: bool = False,
        trusted_source_hosts: Sequence[str] = ("huggingface.co",),
        huggingface_token_path: Path | None = None,
        runtime_archive_available: Callable[[str, int], bool] | None = None,
    ) -> None:
        if not isinstance(root, Path):
            root = Path(root)
        if not root.is_absolute() or any(
            part in {"", ".", ".."} for part in root.parts
        ):
            raise ValueError("model cache root must be an absolute normalized path")
        if root.is_symlink():
            raise ValueError("model cache root must not be a symlink")
        if (
            not isinstance(reserve_bytes, int)
            or isinstance(reserve_bytes, bool)
            or reserve_bytes < 0
        ):
            raise ValueError("model cache reserve must be a non-negative integer")
        if (
            not isinstance(max_parallel_downloads, int)
            or isinstance(max_parallel_downloads, bool)
            or not 1 <= max_parallel_downloads <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise ValueError("model cache parallel downloads must be between 1 and 16")
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
        self._runtime_archive_available = runtime_archive_available
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
        self._active_digests: set[str] = set()
        self._hf_cooldown_until: datetime | None = None
        self._progress_checkpoint_at: dict[str, datetime] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._range_reserved_bytes = 0

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
            provided_specs = tuple(
                self._artifact_from_input(value, model_content_sha256=model_digest)
                for value in artifacts
            )
            manifest = ArtifactSetManifest(
                model_content_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_content_digests=(() if model_digest is None else (model_digest,)),
                artifacts=tuple(sorted(provided_specs, key=lambda item: item.key)),
            )
            _validate_manifest(manifest)
            return manifest

        if (
            model_digest is None
            and recipe_digest is None
            and recipe_revision_id is None
        ):
            raise ModelCacheResolutionError(
                "model_cache.pin_required",
                "an exact model definition or recipe revision is required",
            )
        with self._session() as session:
            recipe_document: RecipeDefinition | None = None
            recipe_model_digests: list[str] = []
            if recipe_digest is not None or recipe_revision_id is not None:
                recipe_document, _resolved_recipe_id, resolved_recipe_digest = (
                    self._recipe_document(session, recipe_digest, recipe_revision_id)
                )
                if (
                    recipe_digest is not None
                    and resolved_recipe_digest != recipe_digest
                ):
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
                if (
                    model_digest is not None
                    and model_digest not in recipe_model_digests
                ):
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

    def _resolve_model_selector(self, selector: str) -> str:
        """Resolve one operator selector to an active model content digest.

        The projection service owns the human-facing list/detail response;
        this mutation boundary still resolves the same canonical identities so
        a request cannot evict a guessed or ambiguous cache entry.  Digests,
        catalog UUIDs, ``publisher/slug`` and an exact slug are accepted.
        """

        selector = _model_selector(selector).casefold()
        with self._session() as session:
            return self._resolve_model_selector_in_session(session, selector)

    @staticmethod
    def _resolve_model_selector_in_session(session: Session, selector: str) -> str:
        """Resolve one current model selector inside its caller's snapshot."""

        if re.fullmatch(_DIGEST_PATTERN, selector):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "model",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest == selector,
                    )
                )
            )
            if not rows:
                cached_digests = list(
                    session.scalars(
                        select(ModelCacheSet.model_content_sha256).where(
                            ModelCacheSet.model_content_sha256 == selector
                        )
                    )
                )
                if cached_digests:
                    return selector
            if rows:
                return selector
        elif re.fullmatch(UUID_PATTERN, selector):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "model",
                        CatalogDocumentRevision.state == "active",
                        (CatalogDocumentRevision.id == selector)
                        | (CatalogDocumentRevision.document_id == selector),
                    )
                )
            )
        else:
            publisher = slug = None
            if "/" in selector:
                publisher, slug = selector.split("/", 1)
            conditions = [
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            ]
            if publisher is not None:
                conditions.append(CatalogDocumentRevision.publisher == publisher)
                conditions.append(CatalogDocumentRevision.slug == slug)
            else:
                conditions.append(CatalogDocumentRevision.slug == selector)
            rows = list(
                session.scalars(select(CatalogDocumentRevision).where(*conditions))
            )
        if len(rows) != 1:
            if not rows:
                raise ModelCacheNotFound(
                    "model_cache.selector_missing", "model selector was not found"
                )
            raise ModelCacheConflict(
                "model_cache.selector_ambiguous",
                "model selector matches multiple models",
            )
        digest = rows[0].content_digest
        if not isinstance(digest, str) or _optional_digest(digest) is None:
            raise ModelCacheResolutionError(
                "model_cache.identity_invalid", "model catalog identity is invalid"
            )
        return digest

    def resolve_latest_cached(
        self,
        *,
        recipe_identity: str,
        model_content_sha256: str | None = None,
        model_variant: str | None = None,
        exact_revision_id: str | None = None,
    ) -> Mapping[str, object]:
        """Resolve one logical Recipe to its newest usable cached revision.

        The profile read/load path calls this method for both web and CLI
        operators.  It is read-only: missing content is reported, never
        downloaded.  A newer active revision may be uncached; in that case we
        return the newest compatible cached receipt and mark that a catalog
        update is available.  If no revision is cached, the newest active
        revision is returned with unknown (``None``) resource estimates so a
        load operation can prepare it explicitly.

        An explicitly selected exact revision is never replaced by an older
        cached one: either the caller names it in ``exact_revision_id``, or
        ``recipe_identity`` is itself a revision id or content digest.  The
        exact revision is returned with ``cached=False`` and a
        ``recipe-not-cached`` blocker when its authorized archive is absent, so
        the operator sees the missing asset instead of a silent substitution.
        """

        if (
            not isinstance(recipe_identity, str)
            or not 1 <= len(recipe_identity.strip()) <= 256
        ):
            raise ModelCacheResolutionError(
                "model_cache.recipe_identity_invalid", "recipe identity is required"
            )
        identity = recipe_identity.strip().casefold()
        requested_model = _optional_digest(model_content_sha256)
        if model_variant is not None and (
            not isinstance(model_variant, str)
            or not 1 <= len(model_variant.strip()) <= 128
        ):
            raise ModelCacheResolutionError(
                "model_cache.model_variant_invalid", "model variant is invalid"
            )
        requested_variant = (
            model_variant.strip() if isinstance(model_variant, str) else None
        )

        with self._session() as session:
            if re.fullmatch(_DIGEST_PATTERN, identity):
                seed = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.content_digest == identity,
                    )
                )
            elif re.fullmatch(UUID_PATTERN, identity):
                seed = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        (CatalogDocumentRevision.id == identity)
                        | (CatalogDocumentRevision.document_id == identity),
                    )
                )
            elif "/" in identity:
                publisher, slug = identity.split("/", 1)
                seed = session.scalar(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.publisher == publisher,
                        CatalogDocumentRevision.slug == slug,
                    )
                    .order_by(CatalogDocumentRevision.revision_number.desc())
                )
            else:
                seed = session.scalar(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.slug == identity,
                    )
                    .order_by(CatalogDocumentRevision.revision_number.desc())
                )
            if seed is None:
                raise ModelCacheResolutionError(
                    "model_cache.recipe_identity_missing",
                    "recipe identity was not found",
                )

            # An explicitly selected exact revision must never be silently
            # replaced by a newer one and must never fall back to an older
            # cached one.
            exact: CatalogDocumentRevision | None = None
            if exact_revision_id is not None:
                if (
                    not isinstance(exact_revision_id, str)
                    or not 1 <= len(exact_revision_id.strip()) <= 256
                ):
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_revision_invalid",
                        "exact recipe revision is invalid",
                    )
                exact = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.id == exact_revision_id.strip(),
                    )
                )
                if exact is None or exact.document_id != seed.document_id:
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_revision_missing",
                        "selected recipe revision was not found",
                    )
            elif re.fullmatch(_DIGEST_PATTERN, identity) or (
                re.fullmatch(UUID_PATTERN, identity) and seed.id == identity
            ):
                exact = seed

            revisions = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.document_id == seed.document_id,
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.revision_number.desc(),
                        CatalogDocumentRevision.id.desc(),
                    )
                )
            )
            if not revisions:
                raise ModelCacheResolutionError(
                    "model_cache.recipe_revision_missing",
                    "recipe has no active revision",
                )
            if exact is not None:
                if exact.state != "active":
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_revision_missing",
                        "selected recipe revision is not active",
                    )
                selection_pool = [exact]
            else:
                selection_pool = revisions

            def compatible_model(
                revision: CatalogDocumentRevision,
            ) -> tuple[str, str | None]:
                recipe = read_catalog_document(revision)
                if not isinstance(recipe, RecipeDefinition):
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_invalid", "recipe revision is not canonical"
                    )
                digests = _recipe_model_content_digests(recipe)
                if requested_model is not None and requested_model not in digests:
                    return "", None
                for digest in digests if requested_model is None else [requested_model]:
                    model_revision = session.scalar(
                        select(CatalogDocumentRevision)
                        .where(
                            CatalogDocumentRevision.kind == "model",
                            CatalogDocumentRevision.content_digest == digest,
                        )
                        .order_by(
                            (CatalogDocumentRevision.state == "active").desc(),
                            CatalogDocumentRevision.revision_number.desc(),
                            CatalogDocumentRevision.id.desc(),
                        )
                    )
                    if model_revision is None:
                        continue
                    model = ModelDefinition.model_validate(
                        read_catalog_document(model_revision)
                    )
                    variant = model.identity.variant
                    if requested_variant is None or variant == requested_variant:
                        return digest, variant
                return "", None

            def verified_image(revision_id: str) -> Mapping[str, object] | None:
                # SQL owns the authorization decision; managed storage owns
                # whether the authorized archive is present. The authorization
                # carries the archive identity, so no receipt row joins them.
                authorizations = session.scalars(
                    select(RuntimeImageAuthorization)
                    .where(
                        RuntimeImageAuthorization.recipe_revision_id == revision_id,
                        RuntimeImageAuthorization.state == "authorized",
                    )
                    .order_by(
                        RuntimeImageAuthorization.authorized_at.desc(),
                        RuntimeImageAuthorization.id.desc(),
                    )
                )
                if self._runtime_archive_available is None:
                    return None
                for authorization in authorizations:
                    archive = authorization.oci_archive_sha256
                    size = authorization.image_bytes
                    if not isinstance(archive, str) or type(size) is not int:
                        raise ModelCacheStorageError(
                            "model_cache.runtime_receipt_invalid",
                            "verified runtime image authorization lacks its archive identity",
                        )
                    if self._runtime_archive_available(archive, size):
                        return {
                            "archive_sha256": archive,
                            "image_bytes": size,
                            "platform_manifest_digest": (
                                authorization.platform_manifest_digest
                            ),
                            "source": authorization.source,
                        }
                return None

            selected: (
                tuple[
                    CatalogDocumentRevision,
                    str,
                    str | None,
                    Mapping[str, object] | None,
                ]
                | None
            ) = None
            newest_compatible: CatalogDocumentRevision | None = None
            for revision in selection_pool:
                digest, variant = compatible_model(revision)
                if not digest:
                    continue
                newest_compatible = newest_compatible or revision
                receipt = verified_image(revision.id)
                if receipt is not None:
                    selected = (revision, digest, variant, receipt)
                    break
            if selected is None:
                if newest_compatible is None:
                    raise ModelCacheConflict(
                        "model_cache.pin_mismatch",
                        "no active recipe revision matches the requested model variant",
                    )
                revision = newest_compatible
                digest, variant = compatible_model(revision)
                receipt = None
            else:
                revision, digest, variant, receipt = selected

            model_rows = list(
                session.scalars(
                    select(ModelCacheSet).where(
                        ModelCacheSet.model_content_sha256 == digest,
                        ModelCacheSet.state == "cached",
                    )
                )
            )

            def model_set_available(row: ModelCacheSet) -> bool:
                manifest = ArtifactSetManifest.from_document(row.manifest)
                if manifest.digest != row.artifact_set_sha256:
                    raise ModelCacheStorageError(
                        "model_cache.manifest_identity_mismatch",
                        "cached artifact manifest does not match its stored identity",
                    )
                required = set(_unique_artifacts(manifest.artifacts))
                return required <= self._managed_cached_objects(manifest)

            model_set = max(
                (row for row in model_rows if model_set_available(row)),
                key=lambda item: (item.updated_at, item.artifact_set_sha256),
                default=None,
            )
            model_cached = model_set is not None
            model_expected = model_set.expected_bytes if model_set is not None else None
            model_verified = model_set.verified_bytes if model_set is not None else None

            recipe_cached = receipt is not None
            image_bytes = receipt["image_bytes"] if receipt is not None else None
            image_digest = (
                receipt["platform_manifest_digest"] if receipt is not None else None
            )
            latest = next(
                (
                    item
                    for item in revisions
                    if item.revision_number > revision.revision_number
                ),
                None,
            )
            additional = (
                model_expected + image_bytes
                if isinstance(model_expected, int) and isinstance(image_bytes, int)
                else None
            )
            blockers = []
            if not recipe_cached:
                blockers.append("recipe-not-cached")
            if not model_cached:
                blockers.append("model-not-cached")
            return {
                "schema_version": SCHEMA_VERSION,
                "recipe": {
                    "recipe_revision_id": revision.id,
                    "document_id": revision.document_id,
                    "publisher": revision.publisher,
                    "slug": revision.slug,
                    "revision_number": revision.revision_number,
                    "content_sha256": revision.content_digest,
                    "cached": recipe_cached,
                    "cache_state": "cached" if recipe_cached else "missing",
                    "artifact_set_sha256": receipt["archive_sha256"]
                    if receipt
                    else None,
                    "expected_bytes": image_bytes,
                    "verified_bytes": image_bytes,
                    "source": receipt["source"] if receipt else None,
                    "image_digest": image_digest,
                    "update_available": latest is not None,
                },
                "model": {
                    "content_sha256": digest,
                    "cached": model_cached,
                    "cache_state": "cached" if model_cached else "missing",
                    "artifact_set_sha256": model_set.artifact_set_sha256
                    if model_set
                    else None,
                    "expected_bytes": model_expected,
                    "verified_bytes": model_verified,
                    "variant": variant,
                },
                "resources": {
                    "per_spark_memory_bytes": None,
                    "additional_disk_bytes": additional,
                    "model_bytes": model_expected,
                    "image_bytes": image_bytes,
                },
                "blockers": blockers,
            }

    def download_model_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_key: str,
        force: bool = False,
    ) -> CacheOperationView:
        """Plan and queue a model download from the operator selector."""

        request_key = _request_key(request_key)
        selector = _model_selector(selector)
        with self._session() as session:
            replay = self._download_replay(
                session, request_key, actor=actor, selector=selector, force=force
            )
            if replay is not None:
                return replay
        digest = self._resolve_model_selector(selector)
        manifest = self.resolve_artifact_set(model_content_sha256=digest)
        preview = self._download_preview_for_manifest(manifest)
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.download_blocked",
                "; ".join(
                    str(item)
                    for item in require_sequence(
                        preview["blockers"], "download blockers"
                    )
                ),
            )
        return self.start_download(
            actor=actor,
            request_key=request_key,
            plan_digest=str(preview["plan_digest"]),
            artifact_set_sha256=manifest.digest,
            model_content_sha256=digest,
            selector=selector,
            force=force,
        )

    def remove_model_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_key: str,
        model_content_sha256: str,
        review_digest: str,
    ) -> CacheOperationView:
        """Accept one exact durable removal without cancelling active work."""

        request_key = _request_key(request_key)
        normalized_selector = _model_selector(selector).casefold()
        digest = _optional_digest(model_content_sha256)
        if digest is None:
            raise ModelCacheResolutionError(
                "model_cache.digest_invalid",
                "model removal requires an exact model content SHA-256",
            )
        supplied_review_digest = _optional_digest(review_digest)
        if supplied_review_digest is None:
            raise ModelCacheResolutionError(
                "model_cache.review_digest_invalid",
                "model removal requires the exact current review digest",
            )
        with self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                return self._replay_model_removal(
                    existing,
                    actor=actor,
                    selector=normalized_selector,
                    model_content_sha256=digest,
                    selected_sets=None,
                    review_digest=supplied_review_digest,
                )

        reviewed = self.review_model_removal(normalized_selector)
        if reviewed.target_identity != digest:
            raise ModelCacheConflict(
                "model_cache.removal_identity_mismatch",
                "model selector no longer resolves to the reviewed content digest",
            )
        if reviewed.review_digest != supplied_review_digest:
            raise ModelCacheConflict(
                "model_cache.removal_review_stale",
                "model cache removal effects changed after review; review them again",
            )
        if reviewed.blockers:
            first = reviewed.blockers[0]
            raise ModelCacheConflict(first.code, first.detail)

        try:
            with self._lock, self._session(write=True) as session:
                existing = session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.request_key == request_key
                    )
                )
                if existing is not None:
                    return self._replay_model_removal(
                        existing,
                        actor=actor,
                        selector=normalized_selector,
                        model_content_sha256=digest,
                        selected_sets=None,
                        review_digest=supplied_review_digest,
                    )
                resolved_digest = self._resolve_model_selector_in_session(
                    session, normalized_selector
                )
                if resolved_digest != digest:
                    raise ModelCacheConflict(
                        "model_cache.removal_identity_mismatch",
                        "model selector no longer resolves to the reviewed content digest",
                    )
                selected_sets = tuple(
                    session.scalars(
                        select(ModelCacheSet.artifact_set_sha256)
                        .where(ModelCacheSet.model_content_sha256 == digest)
                        .order_by(ModelCacheSet.artifact_set_sha256)
                    )
                )
                expected_scope = self._model_removal_scope_for_sets(
                    session, selected_sets
                )
                if not _scope_matches_review(expected_scope, reviewed):
                    raise ModelCacheConflict(
                        "model_cache.removal_review_stale",
                        "model cache removal effects changed after review; review them again",
                    )
                operation = self._accept_model_removal(
                    session,
                    actor=actor,
                    request_key=request_key,
                    selector=normalized_selector,
                    model_content_sha256=digest,
                    selected_sets=None,
                    review_digest=supplied_review_digest,
                    expected_scope=expected_scope,
                )
                operation_id = operation.id
        except IntegrityError:
            # The unique request key arbitrates first submission across
            # Controller processes.  Resolve the winner only after rollback.
            replay = self._model_removal_by_request(
                request_key,
                actor=actor,
                selector=normalized_selector,
                model_content_sha256=digest,
                selected_sets=None,
                review_digest=supplied_review_digest,
            )
            if replay is None:
                raise
            return replay
        return self.get_operation(operation_id)

    def review_model_removal(self, selector: str) -> CacheRemovalReview:
        """Return the current owner-derived model removal impact without writes."""

        normalized_selector = _model_selector(selector).casefold()
        with self._session() as session:
            digest = self._resolve_model_selector_in_session(
                session, normalized_selector
            )
            selected_sets = tuple(
                session.scalars(
                    select(ModelCacheSet.artifact_set_sha256)
                    .where(ModelCacheSet.model_content_sha256 == digest)
                    .order_by(ModelCacheSet.artifact_set_sha256)
                )
            )
            scope = self._model_removal_scope_for_sets(session, selected_sets)
            findings: list[CacheRemovalFinding] = []
            blockers: list[CacheRemovalBlocker] = []
            try:
                with session.begin_nested():
                    by_set = model_set_reference_findings(session, scope.selected_sets)
            except ArtifactLifecycleError as error:
                by_set = {}
                blockers.append(
                    CacheRemovalBlocker(
                        code=error.code,
                        detail=error.detail,
                        retryable=error.retryable,
                        recovery_actions=["retry"] if error.retryable else [],
                    )
                )
            try:
                with session.begin_nested():
                    active_removals = self.removal_owner_findings_in_session(
                        session, scope
                    )
            except ArtifactLifecycleError as error:
                active_removals = ()
                blockers.append(
                    CacheRemovalBlocker(
                        code=error.code,
                        detail=error.detail,
                        retryable=error.retryable,
                        recovery_actions=["retry"] if error.retryable else [],
                    )
                )
            for entries in by_set.values():
                for entry in entries:
                    finding = CacheRemovalFinding(
                        classification=entry.classification,
                        asset_kind=entry.asset.kind,
                        asset_sha256=entry.asset.sha256,
                        owner_kind=entry.owner_kind,
                        owner_id=entry.owner_id,
                        state=entry.state,
                        detail=entry.detail,
                        reason=entry.reason,
                    )
                    findings.append(finding)
                    blockers.append(
                        CacheRemovalBlocker(
                            code="model_cache.removal_referenced",
                            detail=entry.reason,
                            retryable=False,
                            recovery_actions=["resolve_reference"],
                        )
                    )
            findings.extend(active_removals)
            findings.extend(self.retained_model_object_findings(scope))
            blockers.extend(
                CacheRemovalBlocker(
                    code="artifact.deletion_in_progress",
                    detail=finding.reason,
                    retryable=True,
                    recovery_actions=["observe_removal_operation"],
                )
                for finding in active_removals
            )

        assets = self.removal_asset_status(scope)
        content = CacheRemovalReviewContent(
            resource_kind="model",
            selector=normalized_selector,
            target_identity=digest,
            with_model=None,
            assets=list(assets),
            references=[
                item for item in findings if item.classification == "saved-reference"
            ],
            active_work=[
                item for item in findings if item.classification == "active-work"
            ],
            blockers=blockers,
            observed_at=self._clock().isoformat(),
        )
        review = seal_cache_removal_review(content)
        return review

    def removal_owner_findings_in_session(
        self, session: Session, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalFinding, ...]:
        """Project the exact accepted removal owners fencing this model scope.

        The lifecycle gate is the authority for a live deletion fence. Each
        owner is then validated against its durable typed operation intent so
        stale or malformed gate rows fail closed instead of disappearing from
        review output.
        """

        identities = {("model-set", digest) for digest in scope.selected_sets} | {
            ("model-object", digest) for digest in scope.delete_objects
        }
        if not identities:
            return ()
        digests = {digest for _kind, digest in identities}
        gates = tuple(
            session.scalars(
                select(ArtifactLifecycleGate)
                .where(
                    ArtifactLifecycleGate.artifact_kind.in_(
                        ("model-set", "model-object")
                    ),
                    ArtifactLifecycleGate.artifact_sha256.in_(digests),
                    ArtifactLifecycleGate.removal_owner_id.is_not(None),
                )
                .order_by(
                    ArtifactLifecycleGate.artifact_kind,
                    ArtifactLifecycleGate.artifact_sha256,
                )
            )
        )
        selected_gates = [
            gate
            for gate in gates
            if (gate.artifact_kind, gate.artifact_sha256) in identities
        ]
        findings: list[CacheRemovalFinding] = []
        owners: dict[str, tuple[ModelCacheOperation, dict[str, object]]] = {}
        for gate in selected_gates:
            owner_id = gate.removal_owner_id
            if (
                gate.removal_owner_kind != "model-cache-operation"
                or not isinstance(owner_id, str)
                or not owner_id
                or not isinstance(gate.removal_fence, str)
                or not gate.removal_fence
            ):
                raise ArtifactLifecycleError(
                    "artifact.removal_owner_unresolved",
                    "a selected cache identity has an unreadable removal owner; retry after the owner is reconciled",
                    retryable=True,
                )
            cached = owners.get(owner_id)
            if cached is None:
                operation = session.get(ModelCacheOperation, owner_id)
                if operation is None or operation.kind != "remove":
                    raise ArtifactLifecycleError(
                        "artifact.removal_owner_unresolved",
                        "a selected cache identity has no readable removal operation owner",
                        retryable=True,
                    )
                try:
                    payload = _validated_operation_payload(operation)
                except (ModelCacheError, TypeError, ValueError) as error:
                    raise ArtifactLifecycleError(
                        "artifact.removal_owner_invalid",
                        "a selected cache identity has a malformed removal owner",
                        retryable=True,
                    ) from error
                if payload.get("removal_fence") != gate.removal_fence:
                    raise ArtifactLifecycleError(
                        "artifact.removal_owner_invalid",
                        "a selected cache identity removal fence disagrees with its owner",
                        retryable=True,
                    )
                cached = (operation, payload)
                owners[owner_id] = cached
            operation, payload = cached
            selected = payload.get("selected")
            delete_objects = payload.get("delete_objects")
            expected_target = (
                selected if gate.artifact_kind == "model-set" else delete_objects
            )
            if (
                not isinstance(expected_target, list)
                or gate.artifact_sha256 not in expected_target
            ):
                raise ArtifactLifecycleError(
                    "artifact.removal_owner_invalid",
                    "a selected cache identity is not covered by its stored removal plan",
                    retryable=True,
                )
            if operation.state not in {"queued", "running", "partial"}:
                raise ArtifactLifecycleError(
                    "artifact.removal_owner_unresolved",
                    "a selected cache identity remains fenced by a non-active removal owner",
                    retryable=True,
                )
            try:
                identity = ArtifactIdentity(
                    kind=cast(ArtifactKind, gate.artifact_kind),
                    sha256=gate.artifact_sha256,
                )
            except ValueError as error:
                raise ArtifactLifecycleError(
                    "artifact.removal_owner_invalid",
                    "a selected cache identity has an invalid removal gate",
                    retryable=True,
                ) from error
            identity_kind = identity.kind
            identity_digest = identity.sha256
            reason = (
                f"removal operation {operation.id} is {operation.state} and owns "
                f"{identity_kind} {identity_digest}"
            )
            findings.append(
                CacheRemovalFinding(
                    classification="active-work",
                    asset_kind=identity_kind,
                    asset_sha256=identity_digest,
                    owner_kind="model-cache-operation",
                    owner_id=operation.id,
                    state=operation.state,
                    detail="accepted model removal retains this deletion fence",
                    reason=reason,
                )
            )
        return tuple(findings)

    @staticmethod
    def retained_model_object_findings(
        scope: ModelCacheRemovalScope,
    ) -> tuple[CacheRemovalFinding, ...]:
        """Explain retained objects through their exact sibling-set owners."""

        selected_objects = set(scope.selected_objects)
        return tuple(
            CacheRemovalFinding(
                classification="saved-reference",
                asset_kind="model-object",
                asset_sha256=object_digest,
                owner_kind="model-cache-set-membership",
                owner_id=set_digest,
                state=state,
                detail="another cached model set shares this object; removal will retain it",
                reason=f"shared object is retained by model set {set_digest}",
            )
            for object_digest, set_digest, state in scope.shared_memberships
            if object_digest in selected_objects
        )

    def accept_removal_for_sets_in_session(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        selector: str,
        selected_sets: Sequence[str],
    ) -> CacheOperationView:
        """Accept an exact child removal in its recipe parent's transaction."""

        supplied_sets = tuple(selected_sets)
        if not supplied_sets:
            raise ValueError("model removal child requires an exact non-empty scope")
        if len(supplied_sets) != len(set(supplied_sets)):
            raise ValueError("model removal child scope contains duplicate sets")
        normalized_sets = tuple(sorted(supplied_sets))
        for digest in normalized_sets:
            ArtifactIdentity("model-set", digest)
        operation = self._accept_model_removal(
            session,
            actor=actor,
            request_key=_request_key(request_key),
            selector=_model_selector(selector),
            model_content_sha256=None,
            selected_sets=normalized_sets,
        )
        return self._operation_view(operation)

    def accept_recipe_removal_child_in_session(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        recipe_revision_id: str,
        operation_id: str,
        removal_fence: str,
        scope: ModelCacheRemovalScope,
    ) -> tuple[str, str, tuple[str, ...], str] | None:
        """Accept one exact child under gates already reserved by its parent.

        The parent reserves this child's complete identity scope together
        with its own image gates in common kind/digest order. This method
        revalidates that scope and scans protective references in the same
        transaction before it writes the child operation owner.
        """
        if not scope.selected_sets:
            return None
        normalized_key = _request_key(request_key)
        operation = self._accept_model_removal(
            session,
            actor=actor,
            request_key=normalized_key,
            selector=recipe_revision_id,
            model_content_sha256=None,
            selected_sets=scope.selected_sets,
            operation_id=operation_id,
            removal_fence=removal_fence,
            gates_reserved=True,
            expected_scope=scope,
        )
        payload = _validated_operation_payload(operation)
        accepted_sets = tuple(
            str(item)
            for item in require_sequence(payload["selected"], "selected model sets")
        )
        if not isinstance(operation.plan_digest, str):
            raise ModelCacheStorageError(
                "model_cache.removal_plan_invalid",
                "model removal child has no immutable plan digest",
            )
        return operation.id, operation.request_key, accepted_sets, operation.plan_digest

    def recipe_removal_scope_in_session(
        self, session: Session, *, recipe_revision_id: str
    ) -> ModelCacheRemovalScope | None:
        """Resolve the exact currently cached model scope for a recipe revision."""

        recipe, _resolved_id, _recipe_digest = self._recipe_document(
            session, None, recipe_revision_id
        )
        dependency_digests: set[str] = set()
        rows: dict[str, CatalogDocumentRevision] = {}
        for digest in _recipe_model_content_digests(recipe):
            self._collect_model_definitions(session, digest, rows)
        dependency_digests.update(rows)
        if not dependency_digests:
            return None
        selected_sets = tuple(
            session.scalars(
                select(ModelCacheSet.artifact_set_sha256)
                .where(ModelCacheSet.model_content_sha256.in_(dependency_digests))
                .order_by(ModelCacheSet.artifact_set_sha256)
            )
        )
        if not selected_sets:
            return None
        return self._model_removal_scope_for_sets(session, selected_sets)

    @staticmethod
    def _model_removal_scope_for_sets(
        session: Session, selected_sets: Sequence[str]
    ) -> ModelCacheRemovalScope:
        normalized_sets = tuple(sorted(set(selected_sets)))
        objects_by_set = model_set_objects(session, normalized_sets)
        selected_objects = tuple(
            sorted({digest for values in objects_by_set.values() for digest in values})
        )
        shared_memberships = tuple(
            session.execute(
                select(
                    ModelCacheSetArtifact.artifact_sha256,
                    ModelCacheSetArtifact.artifact_set_sha256,
                    ModelCacheSet.state,
                )
                .join(
                    ModelCacheSet,
                    ModelCacheSet.artifact_set_sha256
                    == ModelCacheSetArtifact.artifact_set_sha256,
                )
                .where(
                    ModelCacheSetArtifact.artifact_sha256.in_(selected_objects),
                    ModelCacheSetArtifact.artifact_set_sha256.not_in(normalized_sets),
                )
                .order_by(
                    ModelCacheSetArtifact.artifact_sha256,
                    ModelCacheSetArtifact.artifact_set_sha256,
                )
            )
        )
        external_memberships = {row[0] for row in shared_memberships}
        return ModelCacheRemovalScope(
            selected_sets=normalized_sets,
            memberships=tuple(sorted(objects_by_set.items())),
            selected_objects=selected_objects,
            delete_objects=tuple(
                digest
                for digest in selected_objects
                if digest not in external_memberships
            ),
            shared_memberships=tuple(
                (object_digest, set_digest, state)
                for object_digest, set_digest, state in shared_memberships
            ),
        )

    def removal_asset_status(
        self, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalAsset, ...]:
        """Project exact model removal identities and managed-storage status.

        Manifest and membership reads happen in a short SQL context. That
        context closes before receipt or filesystem observations begin.
        """

        expected_by_set: dict[str, dict[str, int]] = {}
        expected_by_object: dict[str, int] = {}
        memberships_by_set = dict(scope.memberships)
        with self._session() as session:
            for set_digest in scope.selected_sets:
                row = session.get(ModelCacheSet, set_digest)
                if row is None:
                    raise ModelCacheStorageError(
                        "model_cache.removal_scope_unavailable",
                        f"selected model set {set_digest} is no longer available",
                    )
                manifest = ArtifactSetManifest.from_document(row.manifest)
                specs = _unique_artifacts(manifest.artifacts)
                expected = {
                    digest: spec.expected_bytes for digest, spec in specs.items()
                }
                if set(expected) != set(memberships_by_set.get(set_digest, ())):
                    raise ModelCacheStorageError(
                        "model_cache.removal_scope_invalid",
                        f"model set {set_digest} membership disagrees with its manifest",
                    )
                for digest, expected_bytes in expected.items():
                    previous = expected_by_object.setdefault(digest, expected_bytes)
                    if previous != expected_bytes:
                        raise ModelCacheStorageError(
                            "model_cache.removal_scope_invalid",
                            f"model object {digest} has inconsistent expected lengths",
                        )
                expected_by_set[set_digest] = expected

        object_status: dict[str, tuple[AssetAvailability, int | None]] = {}
        for digest, expected_bytes in sorted(expected_by_object.items()):
            try:
                verified_bytes = self._stored_object(digest, expected_bytes)
            except OSError:
                object_status[digest] = ("unknown", None)
                continue
            if verified_bytes is not None:
                object_status[digest] = ("verified", verified_bytes)
                continue
            try:
                metadata = self._object_path(digest).lstat()
            except (FileNotFoundError, NotADirectoryError):
                object_status[digest] = ("missing", 0)
                continue
            except OSError:
                object_status[digest] = ("unknown", None)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                object_status[digest] = ("unknown", None)
                continue
            observed_bytes = metadata.st_size
            if observed_bytes < expected_bytes:
                availability: AssetAvailability = "partial"
            else:
                # Includes an exact-size file without its verified receipt
                # and a file larger than the expected length. Neither is ready.
                availability = "unknown"
            object_status[digest] = (availability, observed_bytes)

        def partial_status(
            set_digest: str, digest: str, expected_bytes: int
        ) -> tuple[AssetAvailability, int | None]:
            partial = self._partial_path(set_digest, digest)
            if expected_bytes >= _PARALLEL_RANGE_MIN_BYTES:
                try:
                    available = range_partial_bytes(
                        partial,
                        expected_bytes,
                        workers=_PARALLEL_RANGE_WORKERS,
                    )
                except (OSError, ValueError):
                    return "unknown", None
                if available == 0:
                    return "missing", 0
                return (
                    "partial" if available < expected_bytes else "unknown",
                    available,
                )
            try:
                metadata = partial.lstat()
            except (FileNotFoundError, NotADirectoryError):
                return "missing", 0
            except OSError:
                return "unknown", None
            if not stat.S_ISREG(metadata.st_mode):
                return "unknown", None
            observed = metadata.st_size
            if observed < expected_bytes:
                return "partial", observed
            return "unknown", observed

        assets: list[CacheRemovalAsset] = []
        delete_objects = set(scope.delete_objects)
        for set_digest, expected in sorted(expected_by_set.items()):
            statuses = []
            for digest, expected_bytes in expected.items():
                final_status = object_status[digest]
                statuses.append(
                    partial_status(set_digest, digest, expected_bytes)
                    if final_status[0] == "missing"
                    else final_status
                )
            expected_bytes = sum(expected.values())
            observed_counts = [count for _availability, count in statuses]
            available_bytes = (
                None
                if any(count is None for count in observed_counts)
                else sum(count for count in observed_counts if count is not None)
            )
            if statuses and all(item[0] == "verified" for item in statuses):
                availability: AssetAvailability = "verified"
            elif statuses and all(item[0] == "missing" for item in statuses):
                availability = "missing"
            elif any(item[0] == "unknown" for item in statuses):
                availability = "unknown"
            elif statuses:
                availability = "partial"
            else:
                availability = "unknown"
            assets.append(
                CacheRemovalAsset(
                    kind="model-set",
                    sha256=set_digest,
                    expected_bytes=expected_bytes,
                    availability=availability,
                    available_bytes=available_bytes,
                    disposition="remove",
                )
            )

        for digest in scope.selected_objects:
            expected_bytes = expected_by_object.get(digest)
            if expected_bytes is None:
                raise ModelCacheStorageError(
                    "model_cache.removal_scope_invalid",
                    f"model object {digest} has no validated manifest length",
                )
            availability, available_bytes = object_status[digest]
            disposition: AssetDisposition = (
                "remove" if digest in delete_objects else "retain-shared"
            )
            assets.append(
                CacheRemovalAsset(
                    kind="model-object",
                    sha256=digest,
                    expected_bytes=expected_bytes,
                    availability=availability,
                    available_bytes=available_bytes,
                    disposition=disposition,
                )
            )
        return tuple(assets)

    def _model_removal_by_request(
        self,
        request_key: str,
        *,
        actor: str,
        selector: str,
        model_content_sha256: str | None,
        review_digest: str | None,
        selected_sets: Sequence[str] | None,
    ) -> CacheOperationView | None:
        with self._session() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if operation is None:
                return None
            return self._replay_model_removal(
                operation,
                actor=actor,
                selector=selector,
                model_content_sha256=model_content_sha256,
                review_digest=review_digest,
                selected_sets=selected_sets,
            )

    def _replay_model_removal(
        self,
        operation: ModelCacheOperation,
        *,
        actor: str,
        selector: str,
        model_content_sha256: str | None,
        review_digest: str | None,
        selected_sets: Sequence[str] | None,
    ) -> CacheOperationView:
        if operation.kind != "remove" or operation.actor != actor:
            raise ModelCacheConflict(
                "model_cache.request_key_reused",
                "request key was already used for another cache operation",
            )
        payload = _validated_operation_payload(operation)
        if (
            payload.get("selector") != selector
            or payload.get("model_content_sha256") != model_content_sha256
            or payload.get("review_digest") != review_digest
            or (
                selected_sets is not None
                and payload.get("selected") != list(selected_sets)
            )
        ):
            raise ModelCacheConflict(
                "model_cache.request_key_reused",
                "request key was already used for another model removal intent",
            )
        return self._operation_view(operation)

    def _accept_model_removal(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        selector: str,
        model_content_sha256: str | None,
        selected_sets: Sequence[str] | None,
        review_digest: str | None = None,
        operation_id: str | None = None,
        removal_fence: str | None = None,
        gates_reserved: bool = False,
        expected_scope: ModelCacheRemovalScope | None = None,
    ) -> ModelCacheOperation:
        existing = session.scalar(
            select(ModelCacheOperation).where(
                ModelCacheOperation.request_key == request_key
            )
        )
        if existing is not None:
            self._replay_model_removal(
                existing,
                actor=actor,
                selector=selector,
                model_content_sha256=model_content_sha256,
                review_digest=review_digest,
                selected_sets=selected_sets,
            )
            return existing

        if selected_sets is None:
            if review_digest is None:
                raise ModelCacheResolutionError(
                    "model_cache.review_digest_invalid",
                    "direct model removal requires its reviewed effect digest",
                )
            selected = tuple(
                session.scalars(
                    select(ModelCacheSet.artifact_set_sha256)
                    .where(ModelCacheSet.model_content_sha256 == model_content_sha256)
                    .order_by(ModelCacheSet.artifact_set_sha256)
                )
            )
        else:
            supplied = tuple(selected_sets)
            if len(supplied) != len(set(supplied)):
                raise ValueError("model removal scope contains duplicate sets")
            selected = tuple(sorted(supplied))
        # Read and validate exact SQL membership before the ordered gate
        # acquisition, then re-read it after the fences are held.
        scope = self._model_removal_scope_for_sets(session, selected)
        if expected_scope is not None and scope != expected_scope:
            raise ModelCacheConflict(
                "artifact.reference_identity_mismatch",
                "model removal scope changed before parent acceptance",
            )
        operation_id = operation_id or str(uuid.uuid4())
        fence = removal_fence or str(uuid.uuid4())
        now = self._clock()
        identities = (
            *(ArtifactIdentity("model-set", digest) for digest in scope.selected_sets),
            *(
                ArtifactIdentity("model-object", digest)
                for digest in scope.delete_objects
            ),
        )
        assignments: tuple[tuple[ArtifactIdentity, RemovalOwnerKind, str, str], ...] = (
            tuple(
                (
                    identity,
                    "model-cache-operation",
                    operation_id,
                    fence,
                )
                for identity in identities
            )
        )
        try:
            if gates_reserved:
                if not removal_fences_match(session, assignments, now=now):
                    raise ArtifactLifecycleError(
                        "artifact.deletion_fence_lost",
                        "parent did not reserve every model identity for this child",
                    )
            else:
                reserve_removal(
                    session,
                    (identity for identity, _kind, _owner, _fence in assignments),
                    owner_kind="model-cache-operation",
                    owner_id=operation_id,
                    fence=fence,
                    now=now,
                )
            locked_scope = self._model_removal_scope_for_sets(session, selected)
            if locked_scope != scope:
                raise ModelCacheConflict(
                    "artifact.reference_identity_mismatch",
                    "model-set membership changed while removal ownership was reserved",
                )
            reasons = model_set_reference_reasons(session, scope.selected_sets)
            blocked = {digest: owners for digest, owners in reasons.items() if owners}
            if blocked:
                first_digest = min(blocked)
                raise ModelCacheConflict(
                    "model_cache.removal_referenced",
                    f"model cache set {first_digest} is still referenced: "
                    + ", ".join(blocked[first_digest][:4]),
                    recovery="retry",
                )
        except ArtifactLifecycleError as error:
            raise ModelCacheConflict(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error

        external_memberships = set(
            session.scalars(
                select(ModelCacheSetArtifact.artifact_sha256).where(
                    ModelCacheSetArtifact.artifact_set_sha256.not_in(
                        scope.selected_sets
                    )
                    if scope.selected_sets
                    else ModelCacheSetArtifact.artifact_set_sha256.is_not(None)
                )
            )
        )
        delete_objects = tuple(
            digest
            for digest in scope.selected_objects
            if digest not in external_memberships
        )
        if delete_objects != scope.delete_objects:
            raise ModelCacheConflict(
                "artifact.reference_identity_mismatch",
                "model object sharing changed while removal ownership was reserved",
            )
        plan = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "selector": selector,
            "model_content_sha256": model_content_sha256,
            "operator_action": "remove-model",
            "review_digest": review_digest,
            "removal_fence": fence,
            "selected": list(scope.selected_sets),
            "selected_objects": list(scope.selected_objects),
            "delete_objects": list(delete_objects),
            "object_index": 0,
            "object_pending_bytes": None,
            "reclaimed_bytes": 0,
            "set_index": 0,
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
            "result": None,
        }
        total_items = len(delete_objects) + len(selected)
        progress = self._model_removal_progress(
            phase="queued",
            total_items=total_items,
            completed_items=0,
            reclaimed_bytes=0,
            current_key=None,
            previous=None,
            now=now,
        )
        operation = ModelCacheOperation(
            id=operation_id,
            request_key=request_key,
            schema_version=SCHEMA_VERSION,
            kind="remove",
            state="queued",
            attempt=1,
            artifact_set_sha256=None,
            plan_digest=_model_removal_intent_digest(
                _removal_document(plan), actor=actor, request_key=request_key
            ),
            payload=_write_operation_payload("remove", plan),
            progress=progress,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
        if not selected:
            self._finish_model_removal_in_session(session, operation, now=now)
        return operation

    def _model_removal_progress(
        self,
        *,
        phase: ModelCacheOperationPhase,
        total_items: int,
        completed_items: int,
        reclaimed_bytes: int,
        current_key: str | None,
        previous: Mapping[str, object] | None,
        now: datetime,
    ) -> dict[str, object]:
        return cache_progress(
            {
                "schema_version": SCHEMA_VERSION,
                "phase": phase,
                "completed_artifacts": completed_items,
                "total_artifacts": total_items,
                "downloaded_bytes": reclaimed_bytes,
                "expected_bytes": None,
                "current_artifact_key": current_key,
            },
            previous=previous,
            now=now,
        )

    @contextmanager
    def _model_storage_lock(
        self, digest: str, *, model_set: bool = False
    ) -> Iterator[None]:
        """Take one stable managed-storage lock without waiting for its owner."""

        ArtifactIdentity("model-set" if model_set else "model-object", digest)
        lock_root = self._root / "locks"
        directory_fd = os.open(
            lock_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        filename = f"model-set-{digest}" if model_set else digest
        try:
            descriptor = os.open(
                filename,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(descriptor, "a+b") as lock_file:
            if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                raise ModelCacheStorageError(
                    "model_cache.lock_unavailable",
                    "managed-cache lock is not a regular file",
                )
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _ArtifactWriterBusy(digest) from None
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _model_object_size(self, digest: str) -> int:
        objects_fd = os.open(
            self._root / "objects",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                shard_fd = os.open(
                    digest[:2],
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=objects_fd,
                )
            except FileNotFoundError:
                return 0
            try:
                try:
                    metadata = os.stat(digest, dir_fd=shard_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return 0
                if not stat.S_ISREG(metadata.st_mode):
                    raise ModelCacheStorageError(
                        "model_cache.removal_path_unsafe",
                        "managed model object is not a regular file",
                    )
                return metadata.st_size
            finally:
                os.close(shard_fd)
        finally:
            os.close(objects_fd)

    def _remove_model_object_files(self, digest: str) -> None:
        """Unlink one exact object and its managed receipt, then fsync its shard."""

        objects_fd = os.open(
            self._root / "objects",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                shard_fd = os.open(
                    digest[:2],
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=objects_fd,
                )
            except FileNotFoundError:
                return
            try:
                for filename in (digest, f"{digest}.receipt.json"):
                    try:
                        metadata = os.stat(
                            filename, dir_fd=shard_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ModelCacheStorageError(
                            "model_cache.removal_path_unsafe",
                            "managed model object or receipt is not a regular file",
                        )
                    os.unlink(filename, dir_fd=shard_fd)
                os.fsync(shard_fd)
            finally:
                os.close(shard_fd)
        finally:
            os.close(objects_fd)

    def _remove_model_partial_set(self, set_digest: str) -> None:
        """Remove one inactive transfer checkpoint through its opened parent."""

        partials_fd = os.open(
            self._root / "partials",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                metadata = os.stat(
                    set_digest, dir_fd=partials_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise ModelCacheStorageError(
                    "model_cache.removal_path_unsafe",
                    "managed model partial checkpoint is not a directory",
                )
            shutil.rmtree(set_digest, dir_fd=partials_fd)
            os.fsync(partials_fd)
        finally:
            os.close(partials_fd)

    def _model_removal_owner_snapshot(
        self,
        operation_id: str,
        *,
        fence: str,
        identity: ArtifactIdentity,
    ) -> dict[str, object] | None:
        with self._session() as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
            ):
                return None
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if (
                operation is None
                or operation.kind != "remove"
                or operation.state not in {"queued", "running", "partial"}
            ):
                return None
            payload = _validated_operation_payload(operation)
            if payload.get("removal_fence") != fence:
                return None
            return payload

    def _persist_model_removal_checkpoint(
        self,
        operation_id: str,
        *,
        fence: str,
        identity: ArtifactIdentity,
        expected_index: int,
        object_step: bool,
        pending_bytes: int | None,
        complete_step: bool,
    ) -> bool:
        now = self._clock()
        with self._session(write=True) as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
            ):
                return False
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if (
                operation is None
                or operation.kind != "remove"
                or operation.state not in {"queued", "running", "partial"}
            ):
                return False
            payload = _validated_operation_payload(operation)
            if payload.get("removal_fence") != fence:
                return False
            index_field = "object_index" if object_step else "set_index"
            if payload.get(index_field) != expected_index:
                return False
            if object_step:
                if pending_bytes is None:
                    raise ModelCacheStorageError(
                        "model_cache.removal_checkpoint_invalid",
                        "model object byte checkpoint is missing",
                    )
                if complete_step:
                    previous_pending = payload.get("object_pending_bytes")
                    if previous_pending != pending_bytes:
                        return False
                    payload["object_index"] = expected_index + 1
                    payload["object_pending_bytes"] = None
                    payload["reclaimed_bytes"] = (
                        require_integer(payload["reclaimed_bytes"], "reclaimed bytes")
                        + pending_bytes
                    )
                else:
                    payload["object_pending_bytes"] = pending_bytes
            elif complete_step:
                payload["set_index"] = expected_index + 1

            payload["retry"] = {"automatic_attempts": 1, "operator_retries": 0}
            payload.pop("failure", None)
            previous = _validated_operation_progress(operation).model_dump(mode="json")
            checkpoint = _removal_document(payload)
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            total_items = len(checkpoint.delete_objects) + len(checkpoint.selected)
            completed_items = object_index + set_index
            current_key: str | None = None
            if object_index < len(checkpoint.delete_objects):
                current_key = f"object:{checkpoint.delete_objects[object_index]}"
            elif set_index < len(checkpoint.selected):
                current_key = f"set:{checkpoint.selected[set_index]}"
            operation.progress = self._model_removal_progress(
                phase="reclaiming",
                total_items=total_items,
                completed_items=completed_items,
                reclaimed_bytes=checkpoint.reclaimed_bytes,
                current_key=current_key,
                previous=previous,
                now=now,
            )
            operation.state = "running"
            operation.last_error = None
            operation.updated_at = now
            operation.payload = _write_operation_payload("remove", payload)
        return True

    def _defer_model_removal(
        self, operation_id: str, *, detail: str, retry_after_seconds: int = 5
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.id == operation_id,
                    ModelCacheOperation.kind == "remove",
                    ModelCacheOperation.state.in_(("queued", "running", "partial")),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return
            payload = _validated_operation_payload(operation)
            retry = payload["retry"]
            if not isinstance(retry, Mapping):
                raise ModelCacheStorageError(
                    "model_cache.payload_invalid",
                    "model removal retry state is missing",
                )
            attempts = int(retry["automatic_attempts"]) + 1
            delay = min(60, max(retry_after_seconds, 2 ** min(attempts, 6)))
            retry_document = dict(retry)
            retry_document.update(
                automatic_attempts=attempts,
                next_retry_at=_iso(now + timedelta(seconds=delay)),
                retry_after_seconds=delay,
            )
            payload["retry"] = retry_document
            checkpoint = _removal_document(payload)
            artifact_key = (
                f"object:{checkpoint.delete_objects[checkpoint.object_index]}"
                if checkpoint.object_index < len(checkpoint.delete_objects)
                else f"set:{checkpoint.selected[checkpoint.set_index]}"
                if checkpoint.set_index < len(checkpoint.selected)
                else "removal-finalization"
            )
            payload["failure"] = _cache_failure(
                "model_cache.removal_wait",
                "Automatic retry resumes this exact checkpoint when its storage or "
                "ownership dependency clears. " + detail,
                retryable=True,
                recovery="inspect",
                retry_time=_iso(now + timedelta(seconds=delay)),
                retry_after_seconds=delay,
                artifact_key=artifact_key,
            )
            previous = _validated_operation_progress(operation).model_dump(mode="json")
            operation.progress = cache_phase(previous, "reclaiming", now, waiting=True)
            operation.state = "partial"
            operation.last_error = redact_text(detail)[:512]
            operation.updated_at = now
            operation.payload = _write_operation_payload("remove", payload)

    def advance_removals(self, *, limit: int = 1) -> int:
        """Advance bounded durable model removals without holding transfer slots."""

        if not 1 <= limit <= 100:
            raise ValueError("model removal batch limit is invalid")
        now = self._clock()
        with self._session() as session:
            operation_ids = tuple(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(
                        ModelCacheOperation.kind == "remove",
                        ModelCacheOperation.state.in_(("queued", "running", "partial")),
                    )
                    .where(
                        or_(
                            ModelCacheOperation.payload["retry"]["next_retry_at"]
                            .as_string()
                            .is_(None),
                            ModelCacheOperation.payload["retry"][
                                "next_retry_at"
                            ].as_string()
                            <= _iso(now),
                        )
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
        advanced = 0
        for operation_id in operation_ids:
            if advanced >= limit:
                break
            try:
                advanced += int(self._advance_model_removal(operation_id, now=now))
            except ModelCacheStorageError as error:
                if error.code not in {
                    "model_cache.payload_invalid",
                    "model_cache.progress_invalid",
                    "model_cache.removal_plan_changed",
                }:
                    self._defer_model_removal(operation_id, detail=error.detail)
                    continue
                # A corrupt owner's document cannot be rewritten into a valid
                # default. Preserve it and its fences for inspection, isolate
                # its failure, and allow unrelated eligible work to continue.
                with self._session(write=True) as session:
                    row = session.scalar(
                        select(ModelCacheOperation)
                        .where(
                            ModelCacheOperation.id == operation_id,
                            ModelCacheOperation.kind == "remove",
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if row is not None:
                        row.state = "failed"
                        row.last_error = f"{error.code}: {error.detail}"[:512]
                        row.updated_at = now
                log_event(
                    _LOGGER,
                    "model_cache.removal_invalid",
                    service="controller",
                    operation_id=operation_id,
                    code=error.code,
                    detail=error.detail,
                )
        return advanced

    def _advance_model_removal(
        self, operation_id: str, *, now: datetime | None = None
    ) -> bool:
        try:
            return self._advance_model_removal_step(operation_id, now=now)
        except (DBAPIError, ArtifactLifecycleError) as error:
            translated = (
                retryable_artifact_database_error(error)
                if isinstance(error, DBAPIError)
                else error
                if error.retryable
                else None
            )
            if translated is None:
                raise
            # The failed transaction and any artifact lock have unwound before
            # recording a retry. A held owner row is skipped, never waited on.
            try:
                self._defer_model_removal(operation_id, detail=translated.detail)
            except DBAPIError as retry_error:
                if retryable_artifact_database_error(retry_error) is None:
                    raise
            return False

    def _advance_model_removal_step(
        self, operation_id: str, *, now: datetime | None = None
    ) -> bool:
        now = now or self._clock()
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None or operation.kind != "remove":
                return False
            if operation.state not in {"queued", "running", "partial"}:
                return False
            payload = _validated_operation_payload(operation)
            retry = payload["retry"]
            if not isinstance(retry, Mapping):
                raise ModelCacheStorageError(
                    "model_cache.payload_invalid",
                    "model removal retry state is missing",
                )
            retry_at = retry.get("next_retry_at")
            if isinstance(retry_at, str) and datetime.fromisoformat(retry_at) > now:
                return False
            checkpoint = _removal_document(payload)
            fence = checkpoint.removal_fence
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            delete_objects = checkpoint.delete_objects
            selected_sets = checkpoint.selected
        if object_index < len(delete_objects):
            digest = str(delete_objects[object_index])
            identity = ArtifactIdentity("model-object", digest)
            try:
                with self._model_storage_lock(digest):
                    current = self._model_removal_owner_snapshot(
                        operation_id, fence=fence, identity=identity
                    )
                    if current is None:
                        return False
                    if current["object_index"] != object_index:
                        return False
                    pending_bytes = _removal_document(current).object_pending_bytes
                    if pending_bytes is None:
                        pending_bytes = self._model_object_size(digest)
                    if not self._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=object_index,
                        object_step=True,
                        pending_bytes=pending_bytes,
                        complete_step=False,
                    ):
                        return False
                    self._remove_model_object_files(digest)
                    return self._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=object_index,
                        object_step=True,
                        pending_bytes=pending_bytes,
                        complete_step=True,
                    )
            except _ArtifactWriterBusy as error:
                self._defer_model_removal(
                    operation_id,
                    detail=error.detail,
                    retry_after_seconds=error.retry_after_seconds or 5,
                )
                return False
            except ArtifactLifecycleError as error:
                if error.retryable:
                    self._defer_model_removal(
                        operation_id, detail=error.detail, retry_after_seconds=5
                    )
                    return False
                raise
            except OSError as error:
                self._defer_model_removal(
                    operation_id, detail=f"{type(error).__name__}: {error}"
                )
                return False
        if set_index < len(selected_sets):
            set_digest = str(selected_sets[set_index])
            identity = ArtifactIdentity("model-set", set_digest)
            try:
                with self._model_storage_lock(set_digest, model_set=True):
                    current = self._model_removal_owner_snapshot(
                        operation_id, fence=fence, identity=identity
                    )
                    if current is None or current["set_index"] != set_index:
                        return False
                    self._remove_model_partial_set(set_digest)
                    return self._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=set_index,
                        object_step=False,
                        pending_bytes=None,
                        complete_step=True,
                    )
            except _ArtifactWriterBusy as error:
                self._defer_model_removal(
                    operation_id,
                    detail=error.detail,
                    retry_after_seconds=error.retry_after_seconds or 5,
                )
                return False
            except ArtifactLifecycleError as error:
                if error.retryable:
                    self._defer_model_removal(
                        operation_id, detail=error.detail, retry_after_seconds=5
                    )
                    return False
                raise
            except OSError as error:
                self._defer_model_removal(
                    operation_id, detail=f"{type(error).__name__}: {error}"
                )
                return False
        with self._session(write=True) as session:
            identities = (
                *(ArtifactIdentity("model-set", str(item)) for item in selected_sets),
                *(
                    ArtifactIdentity("model-object", str(item))
                    for item in delete_objects
                ),
            )
            if not lock_removal_fences(
                session,
                identities,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
                now=now,
            ):
                return False
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if operation is None or operation.kind != "remove":
                return False
            payload = _validated_operation_payload(operation)
            if payload.get("removal_fence") != fence:
                return False
            if operation.state not in {"queued", "running", "partial"}:
                return False
            self._finish_model_removal_in_session(session, operation, now=now)
        return True

    def _finish_model_removal_in_session(
        self, session: Session, operation: ModelCacheOperation, *, now: datetime
    ) -> None:
        payload = _validated_operation_payload(operation)
        checkpoint = _removal_document(payload)
        selected = checkpoint.selected
        delete_objects = checkpoint.delete_objects
        if checkpoint.object_index != len(
            delete_objects
        ) or checkpoint.set_index != len(selected):
            raise ModelCacheStorageError(
                "model_cache.removal_checkpoint_invalid",
                "model removal cannot finish before every target is reconciled",
            )
        fence = str(payload["removal_fence"])
        identities = (
            *(ArtifactIdentity("model-set", str(item)) for item in selected),
            *(ArtifactIdentity("model-object", str(item)) for item in delete_objects),
        )
        try:
            for digest in delete_objects:
                external = session.scalar(
                    select(ModelCacheSetArtifact.artifact_set_sha256)
                    .where(
                        ModelCacheSetArtifact.artifact_sha256 == digest,
                        ModelCacheSetArtifact.artifact_set_sha256.not_in(selected)
                        if selected
                        else ModelCacheSetArtifact.artifact_set_sha256.is_not(None),
                    )
                    .limit(1)
                )
                if external is not None:
                    raise ArtifactLifecycleError(
                        "artifact.reference_scan_failed",
                        "a new model set references a removal target; removal remains fenced",
                    )
            for set_digest in selected:
                session.query(ModelCacheSetArtifact).filter(
                    ModelCacheSetArtifact.artifact_set_sha256 == set_digest
                ).delete(synchronize_session=False)
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    session.delete(row)
            clear_removal(
                session,
                identities,
                owner_kind="model-cache-operation",
                owner_id=operation.id,
                fence=fence,
                now=now,
            )
        except ArtifactLifecycleError as error:
            raise ModelCacheConflict(error.code, error.detail) from error
        result = ModelCacheRemovalResult(
            schema_version=SCHEMA_VERSION,
            removed_entries=list(selected),
            reclaimed_bytes=checkpoint.reclaimed_bytes,
            cancelled_operations=[],
        )
        payload["result"] = result.model_dump(mode="json")
        payload.pop("failure", None)
        previous = _validated_operation_progress(operation).model_dump(mode="json")
        operation.progress = cache_phase(previous, "completed", now)
        operation.payload = _write_operation_payload("remove", payload)
        operation.state = "succeeded"
        operation.last_error = None
        operation.updated_at = now
        operation.completed_at = now

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
        if (
            not isinstance(raw_id, str)
            or not isinstance(raw_path, str)
            or not isinstance(raw_kind, str)
        ):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid",
                "catalog artifact identity is incomplete",
            )
        if (
            not isinstance(raw_digest, str)
            or not isinstance(raw_bytes, int)
            or not isinstance(roles, list)
        ):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid",
                "catalog artifact integrity metadata is incomplete",
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
            revision = None if value.get("revision") is None else str(value["revision"])
            raw_roles = value["roles"]
            if not isinstance(raw_roles, list):
                raise TypeError
            digest = str(value["sha256"])
            expected_bytes = require_integer(value["download_bytes"], "download bytes")
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
        selector: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
        force: bool = False,
        interrupt_after_bytes: int | None = None,
    ) -> CacheOperationView:
        request_key = _request_key(request_key)
        if selector is not None:
            selector = _model_selector(selector)
            with self._session() as session:
                replay = self._download_replay(
                    session, request_key, actor=actor, selector=selector, force=force
                )
                if replay is not None:
                    return replay
        requested_plan = _optional_digest(plan_digest)
        if requested_plan is None:
            raise ModelCacheConflict(
                "model_cache.plan_invalid", "download plan digest is invalid"
            )
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
            replay = self._download_replay(
                session,
                request_key,
                actor=actor,
                selector=selector,
                force=force,
                artifact_set_sha256=set_digest,
                plan_digest=requested_plan,
            )
            if replay is not None:
                return replay
        preview = self._download_preview_for_manifest(manifest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict(
                "model_cache.stale_plan", "download preview is stale"
            )
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.download_blocked",
                "; ".join(
                    str(item)
                    for item in require_sequence(
                        preview["blockers"], "download blockers"
                    )
                ),
            )
        transfer = None if force else preview.get("_transfer")
        if not isinstance(transfer, Mapping):
            transfer = self._transfer_state_for_manifest(manifest, force=force)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifact_set_sha256": set_digest,
            "manifest": manifest.document(),
            "plan_digest": requested_plan,
            "transfer": dict(transfer),
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
            "force_refresh": force,
        }
        if selector is not None:
            payload["selector"] = selector
            payload["operator_action"] = "download-model"
        payload = _write_operation_payload("download", payload)
        try:
            with self._lock, self._session(write=True) as session:
                replay = self._download_replay(
                    session,
                    request_key,
                    actor=actor,
                    selector=selector,
                    force=force,
                    artifact_set_sha256=set_digest,
                    plan_digest=requested_plan,
                )
                if replay is not None:
                    return replay
                else:
                    now = self._clock()
                    self._require_model_sets_open(
                        session,
                        (set_digest,),
                        now=now,
                        object_digests=tuple(
                            item.sha256 for item in manifest.artifacts
                        ),
                    )
                    self._ensure_set(session, manifest)
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
                            expected_bytes=require_integer(
                                transfer["total_bytes"], "transfer total bytes"
                            ),
                        ),
                        actor=actor,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(operation)
                    session.flush()
                    operation_id = operation.id
        except IntegrityError:
            # A borrowed transaction belongs to its caller. Only recover here
            # after our own transaction has rolled back and released its locks.
            if isinstance(self._sessions, Session):
                raise
            with self._session() as session:
                replay = self._download_replay(
                    session,
                    request_key,
                    actor=actor,
                    selector=selector,
                    force=force,
                    artifact_set_sha256=set_digest,
                    plan_digest=requested_plan,
                )
                if replay is None:
                    raise
                return replay
        if interrupt_after_bytes is not None:
            self._run_download(
                operation_id,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        return self.get_operation(operation_id)

    def _download_replay(
        self,
        session: Session,
        request_key: str,
        *,
        actor: str,
        selector: str | None,
        force: bool,
        artifact_set_sha256: str | None = None,
        plan_digest: str | None = None,
    ) -> CacheOperationView | None:
        """Compare original intent, never a refreshed operator preview."""

        existing = session.scalar(
            select(ModelCacheOperation).where(
                ModelCacheOperation.request_key == request_key
            )
        )
        if existing is None:
            return None
        if existing.kind != "download" or existing.actor != actor:
            raise ModelCacheConflict(
                "model_cache.request_key_reused",
                "request key was already used for another cache operation",
            )
        payload = _validated_operation_payload(existing)
        matches = {
            "selector": payload.get("selector") == selector,
            "refresh": payload.get("force_refresh") is force,
        }
        if selector is None:
            matches.update(
                artifact_set=payload.get("artifact_set_sha256") == artifact_set_sha256,
                plan=existing.plan_digest == plan_digest,
            )
        else:
            matches["operator_action"] = (
                payload.get("operator_action") == "download-model"
            )
        if not all(matches.values()):
            raise ModelCacheConflict(
                "model_cache.request_key_reused",
                "request key was already used for another cache operation",
            )
        return self._operation_view(existing)

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
        if (
            requested_set is not None
            and artifacts is None
            and (
                model_content_sha256 is None
                and recipe_revision_sha256 is None
                and recipe_revision_id is None
            )
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
        cached = self._managed_cached_objects(manifest)
        already_cached = sum(
            spec.expected_bytes
            for digest, spec in _unique_artifacts(manifest.artifacts).items()
            if digest in cached
        )
        transfer = self._transfer_state_for_manifest(
            manifest, force=False, cached=cached
        )
        new_bytes = require_integer(transfer["total_bytes"], "transfer total bytes")
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

    def _managed_cached_objects(self, manifest: ArtifactSetManifest) -> frozenset[str]:
        """Read admission metadata for objects verified into managed storage.

        Publication verifies content before atomically placing an object and
        recording its receipt. Admission trusts that receipt across processes
        and restarts, while checking the file is still present and complete.
        Transfers and explicit verification retain their content checks.
        """
        specs = _unique_artifacts(manifest.artifacts)
        # Managed storage owns per-object availability: a verified object is
        # available exactly when its receipt is beside its bytes. SQL is not
        # consulted and holds no availability flag, so a damage or restore that
        # loses the receipt cannot be masked by a stale database row.
        return frozenset(
            sha256
            for sha256, spec in specs.items()
            if self._object_is_available(sha256, spec.expected_bytes)
        )

    def _stored_object_bytes(self, digest: str) -> int:
        """Return a stored object's byte length, or zero when storage lacks it."""

        path = self._object_path(digest)
        try:
            metadata = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return 0
        except OSError:
            return 0
        return metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0

    def _partial_bytes(self, set_digest: str, spec: ArtifactSpec) -> int:
        """Return only a bounded, reusable partial checkpoint length."""
        partial = self._partial_path(set_digest, spec.sha256)
        try:
            if partial.is_symlink():
                return 0
            if spec.expected_bytes >= _PARALLEL_RANGE_MIN_BYTES:
                return range_partial_bytes(
                    partial, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                )
            if not partial.is_file():
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
        cached: frozenset[str] | None = None,
    ) -> dict[str, object]:
        """Create the immutable planned transfer and per-object baselines."""
        artifacts: dict[str, dict[str, int | str | None]] = {}
        total_bytes = 0
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            baseline = (
                0
                if force
                else (
                    spec.expected_bytes
                    if (
                        digest in cached
                        if cached is not None
                        else self._object_is_verified(spec)
                    )
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
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            payload = _validated_operation_payload(operation)
            return dict(require_mapping(payload["transfer"], "cache transfer ledger"))

    def _operation_transfer_snapshot(self, operation_id: str) -> tuple[int | None, int]:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            return self._transfer_totals(_validated_operation_payload(operation))

    def _transfer_state_for_operation(
        self, operation_id: str
    ) -> Mapping[str, object] | None:
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
            # SQL owns membership only. The object's identity, size, and
            # availability are the manifest and the managed-storage receipt.
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
        phase: ModelCacheOperationPhase,
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
                members.append(
                    OperationMemberProgress(
                        member_id=spec.sha256,
                        object_sha256=spec.sha256,
                        phase={
                            "downloading": "download",
                            "verifying": "verify",
                            "completed": "completed",
                        }.get(phase, phase),
                        completed_bytes=min(spec.expected_bytes, baseline + received),
                        total_bytes=spec.expected_bytes,
                        state="succeeded"
                        if phase == "completed" or baseline == spec.expected_bytes
                        else "running",
                    )
                )
        return cache_progress(
            document, previous=previous, now=self._clock(), members=members
        )

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
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            operation_payload = _validated_operation_payload(operation)
            force = bool(operation_payload.get("force_refresh") is True) or force
            operation_set_digest = operation.artifact_set_sha256
            manifest = ArtifactSetManifest.from_document(operation_payload["manifest"])
            set_digest = operation_set_digest
        if set_digest is None:
            raise ModelCacheConflict(
                "model_cache.set_missing", "cache operation has no artifact set"
            )
        transfer = self._ensure_transfer_state(operation_id, manifest, force=force)
        planned_total = transfer.get("total_bytes")
        planned_total = (
            planned_total if type(planned_total) is int and planned_total >= 0 else None
        )
        self._set_operation_state(operation_id, "running")
        completed = 0
        try:
            with self._lock:
                if not self._publication_allowed(operation_id, set_digest):
                    raise InterruptedError(
                        "model download was removed before publication"
                    )
                with self._session(write=True) as session:
                    self._ensure_set(session, manifest)
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
            self._finish_partial(
                operation_id, set_digest, manifest, str(error) or "download interrupted"
            )
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
        with self._lock:
            if spec.sha256 in self._active_digests:
                raise _ArtifactWriterBusy(spec.sha256)
            self._active_digests.add(spec.sha256)
        try:
            # Neither local nor cross-process contention can park a transfer
            # slot. The durable operation is rescheduled after releasing it.
            with (self._root / "locks" / spec.sha256).open("a+b") as lock_file:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise _ArtifactWriterBusy(spec.sha256) from None
                # This small owner hint lets cancellation distinguish this
                # operation's issued effects from another consumer sharing
                # the same artifact lock. The flock remains the authority;
                # the bytes are consulted only while that flock is busy.
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(operation_id.encode("ascii"))
                lock_file.flush()
                self._download_locked(
                    spec,
                    set_digest,
                    operation_id=operation_id,
                    force=force,
                    interrupt_after_bytes=interrupt_after_bytes,
                )
        finally:
            with self._lock:
                self._active_digests.discard(spec.sha256)

    def _download_locked(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            assert operation is not None
            if operation.state == "cancelled" or _operation_cancellation(operation):
                self._transfer_stop(operation_id).set()
                raise InterruptedError(
                    "model download cancellation was accepted; partial files preserved"
                )
            is_repair = operation.kind == "repair"
            checkpoint = (
                ModelCacheRepairCheckpoint.model_validate(
                    _validated_operation_payload(operation)["repair_checkpoint"]
                )
                if force and is_repair
                else None
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
        if is_repair:
            with self._session(write=True) as session:
                operation = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
                assert operation is not None
                payload = _validated_operation_payload(operation)
                checkpoint = ModelCacheRepairCheckpoint.model_validate(
                    payload["repair_checkpoint"]
                )
                payload["repair_checkpoint"] = ModelCacheRepairCheckpoint(
                    transfer_id=checkpoint.transfer_id,
                    completed_objects=list(
                        dict.fromkeys([*checkpoint.completed_objects, spec.sha256])
                    ),
                ).model_dump(mode="json")
                operation.payload = _write_operation_payload("repair", payload)

    def _transfer_stop(self, operation_id: str) -> threading.Event:
        with self._lock:
            return self._cancel_events.setdefault(operation_id, threading.Event())

    def _operation_cancellation_pending(self, operation_id: str) -> bool:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            return bool(
                operation is not None
                and operation.state != "cancelled"
                and _operation_cancellation(operation) is not None
            )

    @contextmanager
    def _sample_transfer(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        operation_id: str,
        completed_artifacts: int,
        initial_bytes: int,
    ):
        """Sample transfer counters independently of socket reads and disk writes."""
        stopped = threading.Event()
        latest = [initial_bytes]
        errors: list[Exception] = []
        cancel = self._transfer_stop(operation_id)

        def sample() -> None:
            while not stopped.wait(1):
                if self._closed.is_set():
                    cancel.set()
                    return
                try:
                    self._checkpoint_artifact(
                        spec,
                        operation_id=operation_id,
                        set_digest=set_digest,
                        actual_bytes=latest[0],
                        state="partial",
                        completed_artifacts=completed_artifacts,
                    )
                except Exception as error:  # noqa: BLE001 - propagate sampler failures to owner
                    errors.append(error)
                    cancel.set()
                    return

        def observe(count: int) -> None:
            latest[0] = count
            if errors:
                raise errors[0]
            if cancel.is_set() or self._closed.is_set():
                raise InterruptedError(
                    "model download stopped; partial files preserved"
                )

        thread = threading.Thread(
            target=sample, name="vonk-model-progress", daemon=True
        )
        thread.start()
        try:
            observe(initial_bytes)
            yield observe
        finally:
            stopped.set()
            thread.join()
            if errors:
                raise errors[0]

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
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            is_repair = operation is not None and operation.kind == "repair"
        if is_repair:
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
            with self._lock:
                if not self._publication_allowed(operation_id, set_digest, spec.sha256):
                    raise InterruptedError(
                        "model download was removed during verification"
                    )
                self._publish_object(spec, part)
                self._mark_artifact_verified(spec, set_digest)
            return
        if offset == spec.expected_bytes:
            part.unlink(missing_ok=True)
            received = 0
            offset = 0
        if (
            spec.expected_bytes >= _PARALLEL_RANGE_MIN_BYTES
            and urlsplit(spec.source).scheme in {"http", "https"}
            and interrupt_after_bytes is None
            and self._download_parallel_ranges(
                spec,
                part,
                set_digest,
                operation_id,
                completed_artifacts,
            )
        ):
            self._complete_download(
                spec, set_digest, part, operation_id, completed_artifacts
            )
            return
        stream, effective_offset, close = self._open_source(spec, offset)
        if effective_offset != offset:
            received = effective_offset
        durable_received = received
        try:
            mode = "ab" if effective_offset else "wb"
            with (
                part.open(mode) as output,
                self._sample_transfer(
                    spec, set_digest, operation_id, completed_artifacts, received
                ) as observe,
            ):

                def sync_received() -> None:
                    nonlocal durable_received
                    output.flush()
                    os.fsync(output.fileno())
                    durable_received = received

                try:
                    while True:
                        observe(received)
                        if isinstance(stream, BufferedReader):
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
                        if (
                            interrupt_after_bytes is not None
                            and received >= interrupt_after_bytes
                        ):
                            raise InterruptedError(
                                "download interrupted at a durable checkpoint"
                            )
                        # Ordinary buffered writes remain independent of progress.
                        # Completion/interruption syncs once; a crash resumes from
                        # the actual retained file length, never a progress counter.
                        observe(received)
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
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=durable_received,
                state="partial",
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
        self._complete_download(
            spec, set_digest, part, operation_id, completed_artifacts
        )

    def _complete_download(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        part: Path,
        operation_id: str,
        completed_artifacts: int,
    ) -> None:
        if self._transfer_stop(operation_id).is_set() or self._closed.is_set():
            raise InterruptedError("model download stopped; partial files preserved")
        received = spec.expected_bytes
        self._checkpoint_artifact(
            spec,
            operation_id=operation_id,
            set_digest=set_digest,
            actual_bytes=received,
            state="verifying",
            completed_artifacts=completed_artifacts,
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
        if (
            self._transfer_stop(operation_id).is_set()
            or self._closed.is_set()
            or self._operation_cancellation_pending(operation_id)
        ):
            raise InterruptedError("model download cancelled during verification")
        # Removal and publication share this process lock.  The durable
        # operation state is checked while holding it, closing the race where
        # a worker verifies an object just as an operator removes its set.
        with self._lock:
            if not self._publication_allowed(operation_id, set_digest, spec.sha256):
                raise InterruptedError("model download was removed during verification")
            self._publish_object(spec, part)
            self._mark_artifact_verified(spec, set_digest)

    def _publication_allowed(
        self, operation_id: str, set_digest: str, object_digest: str | None = None
    ) -> bool:
        try:
            with self._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is None or operation.state == "cancelled":
                    return False
                payload = _validated_operation_payload(operation)
                if payload.get("cancellation") is not None:
                    return False
                if payload.get("removal_fence") is not None:
                    return False
                if session.get(ModelCacheSet, set_digest) is None:
                    return False
                if not reference_gate_is_open_nowait(
                    session, ArtifactIdentity("model-set", set_digest)
                ):
                    return False
                return object_digest is None or reference_gate_is_open_nowait(
                    session, ArtifactIdentity("model-object", object_digest)
                )
        except ArtifactLifecycleError as error:
            if isinstance(self._sessions, Session) or not error.retryable:
                raise
            raise _ArtifactWriterBusy(object_digest or set_digest) from error

    @staticmethod
    def _require_model_sets_open(
        session: Session,
        set_digests: Sequence[str],
        *,
        now: datetime,
        object_digests: Sequence[str] = (),
    ) -> dict[str, tuple[str, ...]]:
        try:
            return require_model_sets_open(
                session,
                set_digests,
                now=now,
                object_digests=object_digests,
            )
        except ArtifactLifecycleError as error:
            raise ModelCacheConflict(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error

    def _validate_http_download(self, spec: ArtifactSpec) -> None:
        try:
            parsed = urlsplit(spec.source)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageError(
                "model_cache.source_invalid", "cache source URL is invalid"
            ) from error
        if not self._fixture_sources and (
            parsed.scheme != "https"
            or hostname is None
            or port is not None
            or hostname.lower().rstrip(".") not in self._trusted_source_hosts
            or _is_private_host(hostname)
        ):
            raise ModelCacheStorageError(
                "model_cache.source_untrusted",
                "production cache downloads require a trusted HTTPS artifact host",
            )

    def _download_parallel_ranges(
        self,
        spec: ArtifactSpec,
        part: Path,
        set_digest: str,
        operation_id: str,
        completed_artifacts: int,
    ) -> bool:
        self._validate_http_download(spec)
        # Range segments and atomic assembly coexist temporarily. Reserve the
        # worst-case additional footprint across this process's active files.
        # The retained prefix is already excluded from current free space:
        # peak total is prefix + 2*object, but growth is at most 2*object.
        # Existing range segments make growth smaller, never larger. A tight
        # disk uses the ordinary sequential stream instead.
        if (
            self._http is not None
            and not self._fixture_sources
            and getattr(self._http, "follow_redirects", False)
        ):
            raise ModelCacheStorageError(
                "model_cache.redirect_forbidden",
                "production cache HTTP clients must not follow redirects",
            )
        reservation = 2 * spec.expected_bytes
        with self._lock:
            free = shutil.disk_usage(self._root).free
            if free - self._reserve_bytes - self._range_reserved_bytes < reservation:
                cleanup_ranges(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                )
                self._checkpoint_artifact(
                    spec,
                    operation_id=operation_id,
                    set_digest=set_digest,
                    actual_bytes=part.stat().st_size if part.exists() else 0,
                    state="partial",
                    completed_artifacts=completed_artifacts,
                )
                return False
            self._range_reserved_bytes += reservation
        client = self._http
        owns_client = client is None
        try:
            if client is None:
                client = httpx.Client(
                    follow_redirects=False, timeout=httpx.Timeout(30.0), trust_env=False
                )

            def open_range(start: int, end: int) -> httpx.Response:
                return self._open_http_response(
                    client,
                    spec.source,
                    {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
                )

            with self._sample_transfer(
                spec,
                set_digest,
                operation_id,
                completed_artifacts,
                range_partial_bytes(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                ),
            ) as observe:
                completed = download_ranges(
                    part,
                    spec.expected_bytes,
                    open_range,
                    self._transfer_stop(operation_id),
                    observe,
                    workers=_PARALLEL_RANGE_WORKERS,
                )
            if not completed:
                self._checkpoint_artifact(
                    spec,
                    operation_id=operation_id,
                    set_digest=set_digest,
                    actual_bytes=part.stat().st_size if part.exists() else 0,
                    state="partial",
                    completed_artifacts=completed_artifacts,
                )
            return completed
        except (OSError, httpx.HTTPError, ValueError, ModelCacheError):
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=range_partial_bytes(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                ),
                state="partial",
                completed_artifacts=completed_artifacts,
            )
            raise
        finally:
            if owns_client and client is not None:
                client.close()
            with self._lock:
                self._range_reserved_bytes -= reservation

    def _open_source(
        self, spec: ArtifactSpec, offset: int
    ) -> tuple[Iterator[bytes] | BufferedReader, int, Callable[[], object]]:
        try:
            parsed = urlsplit(spec.source)
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
                    "model_cache.source_size_mismatch",
                    "cache file source is shorter than its checkpoint",
                )
            handle.seek(offset)
            return handle, offset, handle.close
        self._validate_http_download(spec)
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
                    "model_cache.range_invalid",
                    "cache source returned an invalid byte range",
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
        if source_is_huggingface:
            with self._lock:
                cooldown = self._hf_cooldown_until
            if cooldown is not None and cooldown > self._clock():
                raise ModelCacheStorageError(
                    "model_cache.rate_limited",
                    "Hugging Face download cooldown is active",
                    retry_after_seconds=max(
                        1, int((cooldown - self._clock()).total_seconds())
                    ),
                    recovery="resume",
                )
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
                if source_is_huggingface:
                    until = self._clock() + timedelta(
                        seconds=retry_after or _RETRY_BASE_SECONDS
                    )
                    with self._lock:
                        if (
                            self._hf_cooldown_until is None
                            or until > self._hf_cooldown_until
                        ):
                            self._hf_cooldown_until = until
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
                key: value
                for key, value in self._progress_checkpoint_at.items()
                if 0 <= (now - value).total_seconds() < 1
            }
            with self._session(write=True) as session:
                operation = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
                if operation is not None and (
                    operation.state == "cancelled"
                    or _operation_cancellation(operation) is not None
                ):
                    self._transfer_stop(operation_id).set()
                    return
                if operation is not None and operation.state in {"succeeded", "failed"}:
                    return
                if operation is not None and not force_progress:
                    prior = _validated_operation_progress(operation).measurement
                    if (
                        prior.phase == "download"
                        and prior.observed_at is not None
                        and 0
                        <= (
                            now - datetime.fromisoformat(prior.observed_at)
                        ).total_seconds()
                        < 1
                    ):
                        self._progress_checkpoint_at[operation_id] = now
                        return
                # Refresh bytes belong to the operation's transfer ledger. The
                # last verified receipt and its object remain published until
                # the replacement has been verified and atomically committed.
                if operation is not None:
                    payload = _validated_operation_payload(operation)
                    manifest = ArtifactSetManifest.from_document(payload["manifest"])
                    raw_transfer = payload.get("transfer")
                    transfer = (
                        dict(raw_transfer) if isinstance(raw_transfer, Mapping) else {}
                    )
                    raw_artifacts = transfer.get("artifacts")
                    artifacts = (
                        dict(raw_artifacts)
                        if isinstance(raw_artifacts, Mapping)
                        else {}
                    )
                    raw_entry = artifacts.get(spec.sha256)
                    entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
                    baseline = entry.get("baseline_bytes")
                    baseline = (
                        baseline if type(baseline) is int and baseline >= 0 else 0
                    )
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
                    if (
                        isinstance(claim, Mapping)
                        and claim.get("owner") == self._claim_owner
                    ):
                        payload["claim"] = dict(claim) | {
                            "expires_at": _iso(
                                now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS)
                            )
                        }
                    operation.payload = _write_operation_payload(
                        operation.kind, payload
                    )
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
        # Availability is a managed-storage fact: the receipt beside the bytes
        # owns it. This runs once per object inside the publication lock, so it
        # stays bounded -- the set-level projection is recomputed once when the
        # operation finalizes or the entry is read, never per object, which
        # would make one set quadratic in its own membership.
        self._write_object_receipt(spec, now)
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.updated_at = now

    def _stored_object(self, digest: str, expected_bytes: int) -> int | None:
        """Return the stored bytes when storage holds this exact object.

        One receipt read and one descriptor check answer both "is it
        available" and "how many bytes are there", so a whole-set read costs
        one pass over the objects instead of several.
        """

        if self._read_object_receipt(digest, expected_bytes) is None:
            return None
        try:
            fd = os.open(
                self._object_path(digest),
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None
            raise
        try:
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_bytes:
            return None
        return expected_bytes

    def _verified_bytes(self, session: Session, set_digest: str) -> int:
        row = session.get(ModelCacheSet, set_digest)
        if row is None:
            return 0
        manifest = ArtifactSetManifest.from_document(row.manifest)
        total = 0
        seen: set[str] = set()
        for spec in manifest.artifacts:
            if spec.sha256 in seen:
                continue
            seen.add(spec.sha256)
            stored = self._stored_object(spec.sha256, spec.expected_bytes)
            if stored is not None:
                total += stored
        return total

    def _finish_partial(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        detail: str,
    ) -> None:
        now = self._clock()
        cancellation_pending = False
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is not None and operation.state == "cancelled":
                return
            cancellation_pending = (
                operation is not None and _operation_cancellation(operation) is not None
            )
            if not cancellation_pending:
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "incomplete"
                    row.verified_bytes = self._verified_bytes(session, set_digest)
                    row.updated_at = now
                    row.last_error = detail[:512]
                if operation is not None:
                    operation.state = "partial"
                    operation.last_error = detail[:512]
                    payload = _validated_operation_payload(operation) | {
                        "failure": _cache_failure(
                            "model_cache.interrupted",
                            f"{detail[:480]}; preserved bytes remain available to resume",
                            retryable=True,
                            recovery="resume",
                        )
                    }
                    payload.pop("claim", None)
                    operation.payload = _write_operation_payload(
                        operation.kind, payload
                    )
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
                        transfer=mapping(payload.get("transfer")),
                    )
                    operation.progress = cache_phase(
                        operation.progress, "downloading", now, waiting=True
                    )
                    operation.updated_at = now
        if cancellation_pending:
            self._try_settle_cancellation(operation_id)

    def _defer_artifact_writer(
        self,
        operation_id: str,
        error: _ArtifactWriterBusy,
        artifact_key: str | None,
    ) -> None:
        now = self._clock()
        next_retry = now + timedelta(seconds=_RETRY_BASE_SECONDS)
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update={"nowait": True}
            )
            if operation is None or operation.state != "running":
                return
            payload = _validated_operation_payload(operation)
            if payload.get("cancellation") is not None:
                self._transfer_stop(operation_id).set()
                return
            claim = payload.get("claim")
            if isinstance(claim, Mapping) and (
                claim.get("owner") != self._claim_owner
                or _parse_iso(cast(str, claim["expires_at"])) <= now
            ):
                return
            retry = dict(require_mapping(payload["retry"], "cache retry"))
            retry.update(
                next_retry_at=_iso(next_retry), retry_after_seconds=_RETRY_BASE_SECONDS
            )
            payload.update(
                retry=retry,
                failure=_cache_failure(
                    error.code,
                    error.detail,
                    retryable=True,
                    recovery="resume",
                    retry_time=_iso(next_retry),
                    retry_after_seconds=_RETRY_BASE_SECONDS,
                    artifact_key=artifact_key,
                ),
            )
            payload.pop("claim", None)
            operation.payload = _write_operation_payload(operation.kind, payload)
            operation.state = "queued"
            operation.completed_at = None
            operation.last_error = error.detail
            operation.progress = cache_phase(
                _validated_operation_progress(operation).model_dump(mode="json"),
                "queued",
                now,
                waiting=True,
            )
            operation.updated_at = now

    def _finish_failed(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        error: BaseException,
        failed_artifact_key: str | None = None,
    ) -> None:
        if isinstance(error, _ArtifactWriterBusy):
            self._defer_artifact_writer(operation_id, error, failed_artifact_key)
            return
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
                    measured_required,
                    measured_free,
                    max(0, measured_required - measured_free),
                )
            except (OSError, RuntimeError, ValueError):
                pass
        failure_code = getattr(error, "code", None)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            failure_code = "model_cache.capacity"
        if (
            not isinstance(failure_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", failure_code) is None
        ):
            failure_code = "model_cache.operation_failed"
        cancellation_pending = False
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is not None and operation.state == "cancelled":
                return
            cancellation_pending = (
                operation is not None and _operation_cancellation(operation) is not None
            )
            row = (
                None if cancellation_pending else session.get(ModelCacheSet, set_digest)
            )
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                manifest_document = manifest.document()
                all_valid = all(
                    self._object_is_verified(
                        ArtifactSpec.from_manifest(
                            require_mapping(item, "artifact manifest entry")
                        )
                    )
                    for item in require_sequence(
                        manifest_document["artifacts"], "artifact manifest"
                    )
                )
                row.state = "cached" if all_valid else "needs-repair"
                row.updated_at = now
                row.last_error = detail[:512]
            operation = (
                None
                if cancellation_pending
                else session.get(ModelCacheOperation, operation_id)
            )
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
                bounded_retry = (
                    retryable and automatic_attempts < _MAX_OPERATION_ATTEMPTS
                )
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
                provider_rate_limited = (
                    getattr(error, "code", None) == "model_cache.rate_limited"
                )
                if provider_rate_limited and self._manifest_has_huggingface_source(
                    manifest
                ):
                    self._record_huggingface_cooldown(next_retry)
                failure_payload = _cache_failure(
                    failure_code,
                    detail,
                    retryable=retryable,
                    recovery=getattr(error, "recovery", None)
                    or ("capacity" if failure_code == "model_cache.capacity" else None)
                    or ("resume" if bounded_retry else "retry"),
                    retry_time=_iso(next_retry)
                    if bounded_retry or provider_rate_limited
                    else None,
                    retry_after_seconds=retry_delay
                    if bounded_retry or provider_rate_limited
                    else None,
                    required_bytes=required_bytes,
                    free_bytes=free_bytes,
                    shortfall_bytes=shortfall_bytes,
                    artifact_key=failed_artifact_key,
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
                operation.payload = _write_operation_payload(
                    operation.kind, operation_payload
                )
                operation.progress = cache_phase(
                    _validated_operation_progress(operation).model_dump(mode="json"),
                    "queued" if bounded_retry else "failed",
                    now,
                )
                operation.updated_at = now
        if cancellation_pending:
            self._try_settle_cancellation(operation_id)

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
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
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
                isinstance(failure, Mapping) and failure.get("retryable") is True
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
            if previous.artifact_set_sha256 is None:
                raise ModelCacheConflict(
                    "model_cache.set_missing", "cache operation has no artifact set"
                )
            self._require_model_sets_open(
                session, (previous.artifact_set_sha256,), now=now
            )
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
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
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
            failure_artifact_key = failure.get("artifact_key")
            failed_artifact_key = (
                failure_artifact_key
                if isinstance(failure_artifact_key, str)
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
                error.code,
                safe_detail,
                retryable=False,
                recovery=error.recovery or "check_access_and_resume",
                artifact_key=failed_artifact_key,
            )
            with self._lock, self._session(write=True) as session:
                previous = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
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
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
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
            retry.update(
                automatic_attempts=1, next_retry_at=None, retry_after_seconds=None
            )
            payload["retry"] = retry
            payload["resume_of"] = previous.id
            payload["access_recheck"] = {
                "request_key": request_key,
                "checked_at": _iso(now),
                "authorized": True,
            }
            total, received = self._transfer_totals(payload)
            prior_progress = _validated_operation_progress(previous)
            if previous.artifact_set_sha256 is None:
                raise ModelCacheConflict(
                    "model_cache.set_missing", "cache operation has no artifact set"
                )
            self._require_model_sets_open(
                session, (previous.artifact_set_sha256,), now=now
            )
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
                transfer=mapping(payload.get("transfer")),
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
        exact = (
            next(
                (
                    spec
                    for spec in unique_specs.values()
                    if failed_artifact_key in {spec.key, spec.artifact_id}
                ),
                None,
            )
            if failed_artifact_key
            else None
        )
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
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if operation.state == "cancelled":
                return
            payload = _validated_operation_payload(operation)
            if payload.get("cancellation") is not None and state != "cancelled":
                return
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
        phase: ModelCacheOperationPhase,
        completed_artifacts: int,
        downloaded_bytes: int,
        current_artifact_key: str | None,
        expected_bytes: int | None = None,
        transfer: Mapping[str, object] | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if operation.state == "cancelled":
                return
            payload = _validated_operation_payload(operation)
            if payload.get("cancellation") is not None:
                self._transfer_stop(operation_id).set()
                return
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
                    else mapping(payload.get("transfer"))
                ),
            )
            operation.current_artifact_key = current_artifact_key
            operation.state = "running"
            operation.updated_at = now

    def _artifact_effects_settled(
        self, operation_id: str, manifest: ArtifactSetManifest
    ) -> bool:
        """Check this operation's issued object writers without blocking.

        A lock holder writes its operation ID while holding the flock. A busy
        lock owned by another operation is shared work and does not delay this
        cancellation. Missing or malformed owner bytes are treated
        conservatively because an older or interrupted worker may still hold
        the lock.
        """

        for spec in _unique_artifacts(manifest.artifacts).values():
            try:
                descriptor = (self._root / "locks" / spec.sha256).open("a+b")
            except OSError:
                return False
            with descriptor:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    descriptor.seek(0)
                    owner = descriptor.read(36).decode("ascii", errors="ignore")
                    if not owner or owner == operation_id:
                        return False
                    continue
                # Acquiring the nonblocking lock proves no writer is
                # currently effecting this object. A stale worker that
                # starts later must read the durable cancellation fence.
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True

    def _try_settle_cancellation(self, operation_id: str) -> bool:
        """Finalize a durable cancel intent only after its effects are idle."""

        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if operation.state == "cancelled":
                return True
            cancellation = _operation_cancellation(operation)
            if cancellation is None:
                return False
            if operation.kind != "download" or operation.artifact_set_sha256 is None:
                return False
            payload = _validated_operation_payload(operation)
            manifest = ArtifactSetManifest.from_document(payload["manifest"])
            set_digest = operation.artifact_set_sha256

        if not self._artifact_effects_settled(operation_id, manifest):
            return False

        unique_specs = _unique_artifacts(manifest.artifacts)
        stored_bytes = {
            digest: self._stored_object(digest, spec.expected_bytes)
            for digest, spec in unique_specs.items()
        }
        verified_bytes = sum(value or 0 for value in stored_bytes.values())
        coverage_complete = all(
            stored_bytes[digest] == spec.expected_bytes
            for digest, spec in unique_specs.items()
        )
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if operation.state == "cancelled":
                return True
            current_cancellation = _operation_cancellation(operation)
            if current_cancellation != cancellation:
                return False
            payload = _validated_operation_payload(operation)
            payload.pop("claim", None)
            payload.pop("failure", None)
            operation.payload = _write_operation_payload(operation.kind, payload)
            operation.state = "cancelled"
            operation.completed_at = now
            operation.updated_at = now
            operation.progress = cache_phase(
                _validated_operation_progress(operation).model_dump(mode="json"),
                "completed",
                now,
            )
            operation.current_artifact_key = None
            sibling_work = session.scalar(
                select(func.count())
                .select_from(ModelCacheOperation)
                .where(
                    ModelCacheOperation.artifact_set_sha256 == set_digest,
                    ModelCacheOperation.id != operation_id,
                    ModelCacheOperation.kind.in_(["download", "repair"]),
                    ModelCacheOperation.state.in_(["queued", "running", "partial"]),
                )
            )
            row = session.get(ModelCacheSet, set_digest)
            if row is not None and not sibling_work:
                row.state = "cached" if coverage_complete else "incomplete"
                row.verified_bytes = verified_bytes
                row.updated_at = now
                if coverage_complete:
                    row.verified_at = now
                    row.last_error = None
        return True

    def _reconcile_pending_cancellations(self) -> int:
        """Resume cancellation settlement after a Controller process restart."""

        with self._session() as session:
            operation_ids = list(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(
                        ModelCacheOperation.kind == "download",
                        ModelCacheOperation.state.in_(["queued", "running", "partial"]),
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                )
            )
        settled = 0
        for operation_id in operation_ids:
            if self._operation_cancellation_pending(operation_id):
                settled += int(self._try_settle_cancellation(operation_id))
        return settled

    def _cancellation_intent(
        self, *, actor: str, request_key: str, reason: str
    ) -> ModelCacheCancellation:
        try:
            request = ModelCacheCancellationRequest.model_validate(
                {"request_key": request_key, "reason": reason}
            )
            return ModelCacheCancellation(
                request_key=request.request_key,
                actor=actor,
                reason=request.reason,
                requested_at=_iso(self._clock()) or "",
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelCacheConflict(
                "model_cache.cancellation_invalid",
                "cancellation identity, actor, or reason is invalid",
            ) from error

    def _persist_cancellation(
        self,
        session: Session,
        operation_id: str,
        cancellation: ModelCacheCancellation,
        *,
        preserve_existing_owner: bool,
    ) -> bool:
        operation = session.scalar(
            select(ModelCacheOperation)
            .where(ModelCacheOperation.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise ModelCacheNotFound(
                "model_cache.operation_missing", "cache operation was not found"
            )
        payload = _validated_operation_payload(operation)
        existing = _operation_cancellation(operation)
        if existing is not None:
            stable_fields = ("request_key", "actor", "reason")
            if any(
                existing[field] != getattr(cancellation, field)
                for field in stable_fields
            ):
                if preserve_existing_owner:
                    return False
                raise ModelCacheConflict(
                    "model_cache.cancellation_key_reused",
                    "operation already has a different cancellation request",
                )
            return False
        if (
            operation.kind != "download"
            or operation.state not in {"queued", "running", "partial"}
            or operation.request_key == cancellation.request_key
        ):
            raise ModelCacheConflict(
                "model_cache.not_cancellable",
                "model download is not active or cancellation key conflicts",
            )
        payload["cancellation"] = cancellation.model_dump(mode="json")
        payload.pop("failure", None)
        payload.pop("result", None)
        operation.payload = _write_operation_payload(operation.kind, payload)
        now = self._clock()
        operation.progress = cache_phase(
            _validated_operation_progress(operation).model_dump(mode="json"),
            "cancelling",
            now,
            waiting=True,
        )
        operation.completed_at = None
        operation.updated_at = now
        return True

    def cancel_operation_in_session(
        self,
        session: Session,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        reason: str,
    ) -> bool:
        """Persist a parent-owned cancel intent in its consumer transaction.

        A pre-existing cancellation remains owned by its original actor and
        request. The parent waits for that exact child to settle instead of
        replacing its durable intent.
        """

        cancellation = self._cancellation_intent(
            actor=actor, request_key=request_key, reason=reason
        )
        return self._persist_cancellation(
            session,
            operation_id,
            cancellation,
            preserve_existing_owner=True,
        )

    def signal_cancelled_operation(self, operation_id: str) -> None:
        """Signal and attempt settlement only after the caller's commit."""

        self._transfer_stop(operation_id).set()
        self._try_settle_cancellation(operation_id)

    def cancel_operation(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        reason: str,
    ) -> CacheOperationView:
        """Persist cancellation intent before signalling local transfer workers."""

        cancellation = self._cancellation_intent(
            actor=actor, request_key=request_key, reason=reason
        )
        with self._session(write=True) as session:
            self._persist_cancellation(
                session,
                operation_id,
                cancellation,
                preserve_existing_owner=False,
            )

        # The committed payload is the authority. This process-local event is
        # only a prompt to workers already sampling or reading a source.
        self.signal_cancelled_operation(operation_id)
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> CacheOperationView:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            return self._operation_view(operation)

    def get_operator_operation(
        self, operation_id: str
    ) -> tuple[CacheOperationView, ModelCacheOperatorAction, str]:
        """Return one operator mutation with its stable action and selector."""

        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            if operation.kind not in {"download", "remove"}:
                raise ModelCacheResolutionError(
                    "model_cache.operation_not_observable",
                    "operation is not a current model operator mutation",
                )
            payload = _validated_operation_payload(operation)
            selector = payload.get("selector")
            if not isinstance(selector, str) or not selector:
                raise ModelCacheResolutionError(
                    "model_cache.operation_not_observable",
                    "operator operation has no stable model selector",
                )
            action: ModelCacheOperatorAction = (
                "remove" if operation.kind == "remove" else "download"
            )
            return self._operation_view(operation), action, selector

    def get_operator_request(
        self, request_key: str, *, actor: str
    ) -> tuple[CacheOperationView, ModelCacheOperatorAction, str]:
        """Resolve an operator key without exposing another issuer's binding."""

        request_key = _request_key(request_key)
        with self._session() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key,
                    ModelCacheOperation.actor == actor,
                )
            )
            if (
                operation is None
                or operation.kind not in {"download", "remove"}
                or _validated_operation_payload(operation).get("selector") is None
            ):
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            operation_id = operation.id
        return self.get_operator_operation(operation_id)

    def list_operations(self, *, limit: int = 100) -> tuple[CacheOperationView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            rows = session.scalars(
                select(ModelCacheOperation)
                .order_by(
                    ModelCacheOperation.created_at.desc(), ModelCacheOperation.id.desc()
                )
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
                if _datetime(row.created_at) == boundary_time and row.id == boundary[1]:
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
        cancellation = _operation_cancellation(operation)
        result = payload.get("result")
        failure = ModelCacheService._canonical_failure(operation)
        model_digest = payload.get("model_content_sha256")
        stored_review_digest = payload.get("review_digest")
        view = CacheOperationView(
            id=operation.id,
            request_key=operation.request_key,
            kind=operation.kind,
            state=(
                "cancelling"
                if cancellation is not None and operation.state != "cancelled"
                else operation.state
            ),
            attempt=int(operation.attempt),
            model_content_sha256=(
                model_digest if isinstance(model_digest, str) else None
            ),
            artifact_set_sha256=operation.artifact_set_sha256,
            plan_digest=operation.plan_digest,
            review_digest=(
                stored_review_digest if isinstance(stored_review_digest, str) else None
            ),
            progress=serialize_json_value(progress),  # type: ignore[arg-type]
            result=(
                parse_model_cache_result(operation.kind, result)
                if result is not None
                else None
            ),
            last_error=operation.last_error,
            created_at=_iso(operation.created_at) or "",
            updated_at=_iso(operation.updated_at) or "",
            completed_at=_iso(operation.completed_at),
            retryable=(failure is not None and failure["retryable"] is True),
            failure=failure,
            cancellation=cancellation,
        )
        ModelCacheOperationResponse.model_validate(view, from_attributes=True)
        return view

    @staticmethod
    def _canonical_failure(
        operation: ModelCacheOperation,
    ) -> Mapping[str, object] | None:
        """Read the one current persisted failure contract without repair/defaults."""
        raw = _validated_operation_payload(operation).get("failure")
        return (
            None
            if raw is None
            else AvailabilityOperationFailure.model_validate(raw).model_dump(
                mode="json"
            )
        )

    def resume_operations(self, *, limit: int = 16) -> int:
        """Return durable cache work for the Controller worker to resume.

        Startup must not perform network or disk transfers inline.  The
        worker calls :meth:`tick` after it has claimed its process loop, so an
        API restart only discovers outstanding work here.
        """
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            count = require_integer(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind.in_(["download", "repair", "remove"])
                    )
                    .where(
                        ModelCacheOperation.state.in_(["queued", "running", "partial"])
                    )
                ),
                "cache operation count",
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
        self._reconcile_pending_cancellations()
        rows = self._claim_operations(limit=limit, respect_backoff=False)
        for operation_id, kind in rows:
            with self._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                refresh = bool(
                    operation is not None
                    and isinstance(operation.payload, Mapping)
                    and operation.payload.get("force_refresh") is True
                )
            self._run_download(operation_id, force=kind == "repair" or refresh)
        return len(rows) + self.advance_removals(limit=limit)

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
        self._reconcile_pending_cancellations()
        # Removal steps use the same Controller model-cache worker boundary,
        # but never occupy a transfer slot while waiting: each artifact lock
        # and SQL ownership check is nonblocking and a contended step is
        # durably deferred before this bounded local filesystem action returns.
        removal_steps = self.advance_removals(
            limit=min(requested, self._max_parallel_downloads)
        )
        with self._lock:
            completed = self._advance_background_operations()
            capacity = max(
                0,
                self._max_parallel_downloads
                - sum(
                    len(
                        [
                            future
                            for future in require_sequence(
                                record.get("futures", []), "futures"
                            )
                            if isinstance(future, Future) and not future.done()
                        ]
                    )
                    for record in self._background_operations.values()
                ),
            )
            if not capacity:
                return completed + removal_steps
            claimed = self._claim_operations(
                limit=min(requested, capacity), respect_backoff=True
            )
            for operation_id, kind in claimed:
                with self._session() as session:
                    operation = session.get(ModelCacheOperation, operation_id)
                    refresh = bool(
                        operation is not None
                        and isinstance(operation.payload, Mapping)
                        and operation.payload.get("force_refresh") is True
                    )
                self._schedule_background_download(
                    operation_id,
                    force=kind == "repair" or refresh,
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
                    futures = (
                        require_sequence(record.get("futures", []), "futures")
                        if isinstance(record, Mapping)
                        else []
                    )
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
            return completed + len(claimed) + removal_steps

    def _available_transfer_slots(self) -> int:
        return max(
            0,
            self._max_parallel_downloads
            - sum(
                len(
                    [
                        future
                        for future in require_sequence(
                            record.get("futures", []), "futures"
                        )
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
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            manifest = ArtifactSetManifest.from_document(
                _validated_operation_payload(operation)["manifest"]
            )
            set_digest = operation.artifact_set_sha256
        if set_digest is None:
            raise ModelCacheConflict(
                "model_cache.set_missing", "cache operation has no artifact set"
            )
        transfer = self._ensure_transfer_state(operation_id, manifest, force=force)
        planned_total = transfer.get("total_bytes")
        planned_total = (
            planned_total if type(planned_total) is int and planned_total >= 0 else None
        )
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
        if self._transfer_stop(operation_id).is_set():
            return
        if record is None:
            return
        specs = record["specs"]
        if not isinstance(specs, list):
            return
        futures = record["futures"]
        if not isinstance(futures, list):
            futures = []
            record["futures"] = futures
        pending = sum(
            1 for future in futures if isinstance(future, Future) and not future.done()
        )
        while pending < capacity and require_integer(
            record["next_index"], "next index"
        ) < len(specs):
            spec = specs[require_integer(record["next_index"], "next index")]
            record["next_index"] = (
                require_integer(record["next_index"], "next index") + 1
            )
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
            futures = require_sequence(record.get("futures", []), "futures")
            if not isinstance(futures, list):
                futures = []
            done = [
                future
                for future in futures
                if isinstance(future, Future) and future.done()
            ]
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
            if isinstance(first_error, BaseException):
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
                        recorded_failure_key = record.get("failure_artifact_key")
                        self._finish_failed(
                            operation_id,
                            str(record["set_digest"]),
                            manifest,
                            first_error,
                            failed_artifact_key=(
                                recorded_failure_key
                                if isinstance(recorded_failure_key, str)
                                else None
                            ),
                        )
                self._background_operations.pop(operation_id, None)
                finished += 1
                continue
            specs = require_sequence(record.get("specs", []), "background specs")
            if (
                require_integer(record.get("next_index", 0), "next index") >= len(specs)
                and not futures
            ):
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
                                for current in require_sequence(
                                    item.get("futures", []), "background futures"
                                )
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
                if (
                    operation.state == "cancelled"
                    or operation_payload.get("cancellation") is not None
                ):
                    self._transfer_stop(operation_id).set()
                    record = self._background_operations[operation_id]
                    record["failure"] = InterruptedError(
                        "model download cancellation was accepted"
                    )
                    continue
                claim = operation_payload.get("claim")
                if (
                    isinstance(claim, Mapping)
                    and claim.get("owner") == self._claim_owner
                ):
                    operation_payload["claim"] = dict(claim) | {
                        "expires_at": _iso(
                            now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS)
                        )
                    }
                    operation.payload = _write_operation_payload(
                        operation.kind, operation_payload
                    )
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
        publication_allowed = False
        with self._lock:
            try:
                publication_allowed = self._publication_allowed(
                    operation_id, set_digest
                )
            except _ArtifactWriterBusy as error:
                self._defer_artifact_writer(operation_id, error, None)
                return
            if publication_allowed:
                with self._session(write=True) as session:
                    row = session.get(ModelCacheSet, set_digest)
                    if row is not None:
                        row.state = "cached"
                        row.verified_bytes = manifest.expected_bytes
                        row.verified_at = now
                        row.updated_at = now
                        row.last_accessed_at = now
                        row.last_error = None
        if not publication_allowed:
            self._try_settle_cancellation(operation_id)
            return
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
                        .where(
                            ModelCacheOperation.state.in_(
                                ["queued", "running", "partial", "failed"]
                            )
                        )
                        .order_by(ModelCacheOperation.updated_at.desc())
                        .limit(256)
                    )
                )
                self._refresh_huggingface_cooldown(cooldown_rows, now)
            candidate_ids = list(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                    .where(
                        ModelCacheOperation.state.in_(["queued", "running", "partial"])
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                )
            )
            for operation_id in candidate_ids:
                if len(claimed) >= limit:
                    break
                operation = session.scalar(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.id == operation_id,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
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
                if payload.get("cancellation") is not None:
                    continue
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
                            if (
                                isinstance(expires, str)
                                and datetime.fromisoformat(expires) > now
                            ):
                                continue
                        except ValueError:
                            pass
                        if operation.state == "running":
                            operation.state = "partial"
                payload["claim"] = {
                    "owner": self._claim_owner,
                    "expires_at": _iso(
                        now + timedelta(seconds=_TRANSFER_CLAIM_SECONDS)
                    ),
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
            if not isinstance(retry_at, str):
                continue
            candidate = _datetime(datetime.fromisoformat(retry_at))
            if candidate > now and (latest is None or candidate > latest):
                latest = candidate
        self._hf_cooldown_until = (
            latest if latest is not None and latest > now else None
        )

    def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = self.get_entry(digest)
        artifact_rows = require_sequence(entry["artifacts"], "artifacts")
        artifact_digests = [
            require_mapping(item, "cache artifact entry")["sha256"]
            for item in artifact_rows
        ]
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "repair",
            "artifact_set_sha256": digest,
            "artifacts": artifact_digests,
            "source_policy": SOURCE_POLICY,
        }
        plan_digest = _sha256_json(plan)
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": digest,
            "plan_digest": plan_digest,
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(artifact_rows),
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
            raise ModelCacheConflict(
                "model_cache.stale_plan", "repair preview is stale"
            )
        request_key = _request_key(request_key)
        with self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
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
        if (
            require_integer(transfer["total_bytes"], "transfer total bytes")
            > self.storage_summary().available_bytes
        ):
            raise ModelCacheConflict(
                "model_cache.download_blocked", "insufficient-reserved-storage"
            )
        payload = {
            "repair_checkpoint": ModelCacheRepairCheckpoint(
                transfer_id=uuid.uuid4().hex, completed_objects=[]
            ).model_dump(mode="json"),
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
                now = self._clock()
                self._require_model_sets_open(session, (digest,), now=now)
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
                        expected_bytes=require_integer(
                            transfer["total_bytes"], "transfer total bytes"
                        ),
                    ),
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _manifest_for_set(self, digest: str) -> ArtifactSetManifest:
        with self._session() as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                raise ModelCacheNotFound(
                    "model_cache.entry_missing", "cache entry was not found"
                )
            manifest = ArtifactSetManifest.from_document(row.manifest)
            if manifest.digest != digest:
                raise ModelCacheConflict(
                    "model_cache.identity_conflict",
                    "persisted cache manifest does not match its artifact-set identity",
                )
            return manifest

    def manifest_for_artifact_set(
        self, artifact_set_sha256: str
    ) -> ArtifactSetManifest:
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
        expected_bytes = require_integer(entry["expected_bytes"], "expected bytes")
        verified_bytes = require_integer(entry["verified_bytes"], "verified bytes")
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
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Return cache operations for the global Activity provider seam.

        Cache work is Controller/NAS scoped and therefore has no Spark node
        IDs.  A node filter consequently returns an empty page while still
        reporting the unfiltered-by-cursor total for the requested state.
        ``after`` is the already authenticated global activity boundary.
        """
        from .operation_api import _activity_keyset_filter

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
            if request_id is not None:
                filters.append(ModelCacheOperation.request_key == request_id)
            boundary = None if after is None else (_datetime(after[0]), after[1])
            keyset = _activity_keyset_filter(
                ModelCacheOperation.created_at, ModelCacheOperation.id, "", boundary
            )
            if keyset is not None:
                filters.append(keyset)
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
            if request_id is not None:
                total_filters.append(ModelCacheOperation.request_key == request_id)
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

    def _require_managed_cache_coverage(self, manifest: ArtifactSetManifest) -> None:
        if self._managed_cached_objects(manifest) != frozenset(
            spec.sha256 for spec in manifest.artifacts
        ):
            raise ModelCacheConflict(
                "model_cache.coverage_incomplete",
                "cache artifact set is not completely verified",
            )

    def verified_artifact_file(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
    ) -> tuple[Path, int, str]:
        """Verify the requested object's bytes in a complete managed cache set.

        This is the Controller-to-agent serving seam.  The caller receives a
        content-addressed path and must stream it from the returned file
        descriptor/path; no caller-controlled filesystem path is accepted.
        """
        set_digest = _optional_digest(artifact_set_sha256)
        object_digest = _optional_digest(artifact_sha256)
        if (
            set_digest is None
            or object_digest is None
            or not _valid_relative_path(artifact_path)
        ):
            raise ModelCacheNotFound(
                "model_cache.artifact_missing", "verified cache artifact was not found"
            )
        manifest = self._manifest_for_set(set_digest)
        self._require_managed_cache_coverage(manifest)
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

        Compilation and distribution trust durable publication receipts and
        check managed file metadata once. Serving an object separately verifies
        that object's bytes; describing a set must not scan every model file.
        No source URL or caller-controlled path is exposed by this adapter.
        """
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise ModelCacheNotFound(
                "model_cache.entry_missing", "cache entry was not found"
            )
        manifest = self._manifest_for_set(digest)
        self._require_managed_cache_coverage(manifest)
        descriptors = []
        for spec in manifest.artifacts:
            path = self._object_path(spec.sha256)
            size, object_digest = spec.expected_bytes, spec.sha256
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
                raise ModelCacheNotFound(
                    "model_cache.entry_missing", "cache entry was not found"
                )
            self._refresh_protection(session, row)
            # The set projection is recomputed when an entry is read rather
            # than after every object, so a partially completed operation
            # still reports exact stored bytes without making publication
            # quadratic in the set's own membership.
            row.verified_bytes = self._verified_bytes(session, row.artifact_set_sha256)
            manifest = ArtifactSetManifest.from_document(row.manifest)
            artifacts = []
            unique_bytes = 0
            seen: set[str] = set()
            for spec in manifest.artifacts:
                # One owner per fact: managed storage decides availability, and
                # the same descriptor check reports the stored length, so a
                # receipt alone cannot invent bytes.
                stored = self._stored_object(spec.sha256, spec.expected_bytes)
                actual = stored or 0
                state = "verified" if stored is not None else "missing"
                if stored is not None and spec.sha256 not in seen:
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
                    select(ModelCacheSet).order_by(
                        ModelCacheSet.updated_at.desc(),
                        ModelCacheSet.artifact_set_sha256.desc(),
                    )
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
                    "model_cache.cursor_invalid",
                    "cache inventory cursor boundary is stale",
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
            # Object availability lives in managed storage. Reconciliation
            # repairs a receipt from verified bytes, and removes the receipt of
            # an object whose bytes are gone so admission cannot admit it.
            with self._session() as session:
                sets = list(session.scalars(select(ModelCacheSet)))
                expected = {
                    spec.sha256: spec
                    for row in sets
                    for spec in ArtifactSetManifest.from_document(
                        row.manifest
                    ).artifacts
                }
            for sha256, spec in expected.items():
                path = self._object_path(sha256)
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    self._receipt_path(sha256).unlink(missing_ok=True)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    self._receipt_path(sha256).unlink(missing_ok=True)
                    continue
                available = self._object_is_available(sha256, spec.expected_bytes)
                if not available:
                    self._receipt_path(sha256).unlink(missing_ok=True)
                if (
                    not available
                    and metadata.st_size == spec.expected_bytes
                    and verified_files.verify_path(path, sha256, spec.expected_bytes)
                ):
                    self._write_object_receipt(spec, self._clock())
            with self._session(write=True) as session:
                sets = list(session.scalars(select(ModelCacheSet)))
                for row in sets:
                    previous = (
                        row.state,
                        row.verified_bytes,
                        row.protected,
                        row.protected_reasons,
                    )
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
                    if previous != (
                        row.state,
                        row.verified_bytes,
                        row.protected,
                        row.protected_reasons,
                    ):
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
                    RecipeInstallation.state.in_(
                        ["planned", "installing", "installed", "partial"]
                    ),
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
            return (
                {row.model_content_sha256}
                if row.model_content_sha256 is not None
                else set()
            )
        return set(manifest.model_content_digests) or (
            {row.model_content_sha256}
            if row.model_content_sha256 is not None
            else set()
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
            if (
                revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
            ):
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
            recipe_update = (
                latest_recipe is not None
                and latest_recipe != row.recipe_revision_sha256
            )
        return model_update, recipe_update

    @staticmethod
    def _model_update_candidate(
        session: Session, manifest: ArtifactSetManifest
    ) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision] | None:
        current, candidates = ModelCacheService._model_update_candidates(
            session, manifest
        )
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
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.publisher == ref.publisher,
                    CatalogDocumentRevision.slug == ref.slug,
                    CatalogDocumentRevision.state == "active",
                )
                .order_by(CatalogDocumentRevision.revision_number.asc())
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
            same_lineage = (
                _model_lineage_signature(candidate_document) == current_signature
            )
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
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.document_id == current.document_id,
                CatalogDocumentRevision.state == "active",
                active_head_revision(),
            )
        )
        return None if latest is None else latest.content_digest

    def _check_upstream_revision(
        self, repository: str, revision: str
    ) -> dict[str, object]:
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
                client,
                f"https://huggingface.co/api/models/{repository}/revision/main",
                {},
            )
            try:
                response.read()
                document = response.json()
            finally:
                response.close()
            latest = document.get("sha") if isinstance(document, dict) else None
            if not isinstance(latest, str) or not re.fullmatch(
                r"[0-9a-f]{40,64}", latest
            ):
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
            result["error_code"] = getattr(
                error, "code", "model_cache.upstream_check_failed"
            )
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
                    future = self._upstream_executor.submit(
                        self._check_upstream_revision, *key
                    )
                except RuntimeError:
                    self._upstream_slots.release()
                    break
                future.add_done_callback(lambda _: self._upstream_slots.release())
                pending[future] = key
            if not pending:
                break
            done, _ = wait(
                pending,
                timeout=max(0, deadline - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                key = pending.pop(future)
                try:
                    results[key] = future.result()
                except Exception:  # noqa: BLE001 - diagnostics must not hide local catalog results
                    # A diagnostic/provider bug cannot hide the catalog page.
                    results[key] = {
                        "repository": key[0],
                        "pinned_revision": key[1],
                        "latest_revision": None,
                        "status": "check-failed",
                        "checked_at": _iso(self._clock()),
                        "error_code": "model_cache.upstream_check_failed",
                    }
        for future in pending:
            future.cancel()
        for repository, revision in ordered:
            results.setdefault(
                (repository, revision),
                {
                    "repository": repository,
                    "pinned_revision": revision,
                    "latest_revision": None,
                    "status": "check-failed",
                    "checked_at": _iso(self._clock()),
                    "error_code": "model_cache.upstream_check_budget_exhausted",
                },
            )
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
                current_model, candidates = self._model_update_candidates(
                    session, manifest
                )
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
                        repository = "/".join(
                            urlsplit(spec.source).path.strip("/").split("/")[:2]
                        )
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
            upstream_checks = self._check_upstream_revisions(
                [key for sources in upstream_sources.values() for key in sources]
            )
            for entry in result:
                entry["upstream_revisions"] = [
                    upstream_checks[key]
                    for key in upstream_sources[entry["artifact_set_sha256"]]
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
        # Partial checkpoints are storage facts. They are measured before the
        # read transaction opens so no SQL session spans the directory scan.
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
            in_flight_artifacts: dict[str, int] = {}
            for operation in operations:
                if operation.kind not in {"download", "repair"}:
                    continue
                payload = _validated_operation_payload(operation)
                manifest = ArtifactSetManifest.from_document(payload["manifest"])
                for item in manifest.artifacts:
                    in_flight_artifacts.setdefault(item.sha256, item.expected_bytes)
            protected_bytes = sum(
                object_bytes.get(digest, 0) for digest in protected_artifacts
            )
            # Any on-disk object that is not protected can be reclaimed.  This
            # includes orphaned files left by an interrupted atomic publish;
            # reporting physical bytes keeps capacity decisions honest.
            reclaimable_bytes = sum(
                size
                for digest, size in object_bytes.items()
                if digest not in protected_artifacts
            )
            in_flight_bytes = sum(
                max(
                    0,
                    expected
                    - max(
                        object_bytes.get(digest, 0),
                        partial_bytes.get(digest, 0),
                        self._stored_object_bytes(digest),
                    ),
                )
                for digest, expected in in_flight_artifacts.items()
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

    def _receipt_path(self, digest: str) -> Path:
        return self._root / "objects" / digest[:2] / f"{digest}.receipt.json"

    def _write_object_receipt(self, spec: ArtifactSpec, verified_at: datetime) -> None:
        """Publish the managed-storage receipt that owns an object's availability.

        The receipt is written after the verified bytes are in place, and it is
        replaced atomically so a reader never sees a partial document.
        """

        receipt = ModelCacheObjectReceipt(
            schema_version=SCHEMA_VERSION,
            sha256=spec.sha256,
            storage_key=self._object_key(spec.sha256),
            expected_bytes=spec.expected_bytes,
            actual_bytes=spec.expected_bytes,
            verified_at=verified_at.isoformat(),
        )
        path = self._receipt_path(spec.sha256)
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        receipt.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)

    def _read_object_receipt(
        self, digest: str, expected_bytes: int
    ) -> ModelCacheObjectReceipt | None:
        """Read a receipt and confirm it still describes the object on disk.

        Missing, malformed, or mismatched receipts all mean "not available":
        admission must never infer availability from bytes alone, and a
        damaged receipt is repaired through the normal prepare/verify path
        rather than trusted.
        """

        path = self._receipt_path(digest)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        try:
            receipt = ModelCacheObjectReceipt.model_validate(document)
        except ValidationError:
            return None
        if receipt.sha256 != digest or receipt.expected_bytes != expected_bytes:
            return None
        return receipt

    def _object_is_available(self, digest: str, expected_bytes: int) -> bool:
        """Whether managed storage holds a verified object with its receipt."""

        return self._stored_object(digest, expected_bytes) is not None

    def _partial_path(self, set_digest: str, digest: str) -> Path:
        return self._root / "partials" / set_digest / f"{digest}.part"


def _model_selector(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
        raise ModelCacheResolutionError(
            "model_cache.selector_invalid", "model selector is required"
        )
    return value.strip()


def _request_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ModelCacheConflict(
            "model_cache.request_key_invalid", "request key is invalid"
        ) from error


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
    return host == _HF_CANONICAL_HOST or host.endswith((".huggingface.co", ".hf.co"))


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


def _contains_digest(
    value: object, model_digest: str | None, recipe_digest: str | None
) -> bool:
    if model_digest is None and recipe_digest is None:
        return False
    if isinstance(value, Mapping):
        return any(
            _contains_digest(child, model_digest, recipe_digest)
            for child in value.values()
        ) or (
            (
                model_digest is not None
                and value.get("model_content_sha256") == model_digest
            )
            or (
                recipe_digest is not None
                and value.get("recipe_revision_sha256") == recipe_digest
            )
        )
    if isinstance(value, list):
        return any(
            _contains_digest(child, model_digest, recipe_digest) for child in value
        )
    return False


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
