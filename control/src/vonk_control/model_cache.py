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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import BufferedReader
from pathlib import Path
from typing import ClassVar, cast
from urllib.parse import unquote, urljoin, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, object_session, sessionmaker
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    AssetAvailability,
    CacheReferenceReason,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheBlockerCode,
    ModelCacheCode,
    ModelFileState,
    OperationMemberProgress,
    ProgressPhase,
    RunState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    adopt_progress_phase,
    canonical_message,
    input_state,
)
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.model import GitHubReleaseSource, ModelReference

from . import model_cache_states
from .agent_operation_facts import aware as _aware
from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactKind,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    check_removal_fence_nowait,
    clear_removal,
    dead_removal_identities,
    has_pending_removal,
    lock_removal_fences,
    reference_gate_is_open_nowait,
    release_dead_removal_nowait,
    removal_fences_match,
    reserve_removal,
    retryable_artifact_database_error,
    supersede_removal_nowait,
)
from .artifact_reference_scan import (
    model_set_objects,
    model_set_reference_findings,
    model_set_reference_reasons,
    require_model_sets_open,
)
from .bounded_json import require_integer, require_mapping, require_sequence
from .cache_removal_review import (
    AssetDisposition,
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    refusing_removal_blockers,
    seal_cache_removal_review,
)
from .cached_file_verification import verified_files
from .catalog_queries import active_head_revision
from .catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
)
from .categorized_errors import InvalidType, InvalidValue
from .categorized_faults import OperationInterrupted
from .failure_classification import is_security_failure
from .lifecycle import (
    CancelRequested,
    Effect,
    Lifecycle,
    Outcome,
    Reconciler,
    Reported,
    State,
    Tick,
)
from .lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from .lifecycle.model_cache import (
    ModelCacheAdapter,
    ModelCacheStore,
    legacy_retry_due,
)
from .logging import log_event, redact_text
from .machine_states import INSTALLATION_ACTIVE
from .model_cache_contract import (
    UUID_PATTERN,
    CachedModelResolution,
    CachedRecipeResolution,
    CachedResourceEstimate,
    CacheIdentity,
    CacheIdentityArtifact,
    CacheManifest,
    CacheManifestArtifact,
    CacheManifestArtifactPart,
    CacheResolution,
    ModelCacheAccessRecheck,
    ModelCacheCancellation,
    ModelCacheCancellationRequest,
    ModelCacheCounters,
    ModelCacheDownloadPayload,
    ModelCacheDownloadResult,
    ModelCacheMissingSourceObservation,
    ModelCacheObjectReceipt,
    ModelCacheOperationPayload,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
    ModelCacheOperationResponse,
    ModelCacheOperationResult,
    ModelCacheOperatorAction,
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    ModelCacheRepairCheckpoint,
    ModelCacheRepairPayload,
    ModelCacheRetry,
    ModelCacheTransfer,
    ModelCacheTransferArtifact,
    parse_model_cache_payload,
    parse_model_cache_result,
)
from .model_cache_progress import (
    PHASES,
    cache_phase,
    cache_progress,
    progress_document,
)
from .model_cache_ranges import cleanup_ranges, download_ranges, range_partial_bytes
from .model_cache_streams import StreamGovernor
from .models import (
    ArtifactLifecycleGate,
    CatalogDocumentRevision,
    FleetProfile,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
)
from .operation_blockers import (
    OperationBlocker,
    bound_blockers,
    make_blocker,
)
from .operation_contract import AvailabilityOperationFailure
from .revision_images import RevisionImage, revision_images
from .runtime_init import RuntimeSecretError, read_runtime_secret
from .storage_demands import NAS_MODELS, StorageDemands
from .stored_json import read_row_column
from .strict_json import read_stored_model, serialize_json_value
from .worker_memory_contract import WorkerMemoryComponent

SCHEMA_VERSION = 2
SOURCE_POLICY = "nas-first"
_DIGEST_LENGTH = 64
_DIGEST_PATTERN = r"[0-9a-f]{64}"
_MAX_ARTIFACTS = 1024
_MAX_ARTIFACT_PARTS = 1024
_MAX_MANIFEST_BYTES = 1_048_576
_CHUNK_BYTES = 1024 * 1024
_PARALLEL_RANGE_MIN_BYTES = 64 * 1024 * 1024
_PARALLEL_RANGE_WORKERS = 4
_MAX_HTTP_REDIRECTS = 3
_DEFAULT_MAX_PARALLEL_DOWNLOADS = 16
_MAX_PARALLEL_DOWNLOADS = 32
_DEFAULT_MAX_DOWNLOAD_STREAMS = 16
_RETRY_BASE_SECONDS = 5
# A 404/410 from the provider is retried a bounded number of times (a CDN or
# mirror can answer it transiently); past that the file is gone upstream and the
# download ends with a typed reason instead of waiting forever.
_SOURCE_GONE_STATUSES = frozenset({404, 410})
_SOURCE_GONE_ATTEMPTS = 5
#: The actor of a download the cache queues by itself to re-verify a set.
_REVERIFY_ACTOR = "system:model-cache-reverify"
_MAX_RETRY_HINT_SECONDS = 365 * 24 * 60 * 60
_TRANSFER_CLAIM_SECONDS = 120
_UPSTREAM_CHECK_SECONDS = 8.0
_UPSTREAM_CHECK_WORKERS = 4
_HF_CANONICAL_HOST = "huggingface.co"
_GITHUB_API_HOST = "api.github.com"
_GITHUB_USER_AGENT = "vonk-forge/0.1.1"
_GITHUB_RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"
_MAX_GITHUB_RELEASE_METADATA_BYTES = 4 * 1024 * 1024
_MAX_GITHUB_ERROR_METADATA_BYTES = 64 * 1024
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
        source_status: int | None = None,
    ) -> None:
        self.code = code
        #: The provider's HTTP status when the failure is an answer from it.
        self.source_status = source_status
        #: The manifest entry whose transfer raised this, set by its transfer.
        self.failed_artifact_key: str | None = None
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


def _refusal_reason(code: str) -> SecurityRefusalReason | None:
    """The contract reason a raise names by its code, when the code is one."""

    try:
        return SecurityRefusalReason(code)
    except ValueError:
        return None


class _CacheRefusal(SecurityRefusalError, ModelCacheError):
    """A refusal at a security boundary (credentials, source trust, artifact
    identity, a destructive-effect guard): fails closed."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: SecurityRefusalReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
        )
        self.typed_reason = reason if reason is not None else _refusal_reason(code)


class _CacheInvalid(InvalidRequestError, ModelCacheError):
    """A malformed, stale or out-of-contract request: rejected before effects."""

    default_reason: ClassVar[InvalidRequestReason] = InvalidRequestReason.MALFORMED

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: InvalidRequestReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
        )
        self.typed_reason = reason if reason is not None else self.default_reason
        self.typed_field = None


class _CacheUnknown(UnknownOutcomeError, ModelCacheError):
    """A busy owner, an unavailable source or unconfirmed bookkeeping: the worker
    observes and retries; it is never a refusal."""

    default_reason: ClassVar[WaitReason | None] = None

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: WaitReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
        source_status: int | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
            source_status=source_status,
        )
        self.typed_reason = reason if reason is not None else self.default_reason


class ModelCacheConflictRefused(_CacheRefusal, ModelCacheConflict):
    pass


class ModelCacheConflictInvalid(_CacheInvalid, ModelCacheConflict):
    default_reason = InvalidRequestReason.CONFLICT


class ModelCacheConflictUnknown(_CacheUnknown, ModelCacheConflict):
    pass


class ModelCacheNotFoundRefused(_CacheRefusal, ModelCacheNotFound):
    pass


class ModelCacheNotFoundInvalid(_CacheInvalid, ModelCacheNotFound):
    default_reason = InvalidRequestReason.NOT_FOUND


class ModelCacheResolutionRefused(_CacheRefusal, ModelCacheResolutionError):
    pass


class ModelCacheResolutionInvalid(_CacheInvalid, ModelCacheResolutionError):
    pass


class ModelCacheStorageRefused(_CacheRefusal, ModelCacheStorageError):
    pass


class ModelCacheStorageInvalid(_CacheInvalid, ModelCacheStorageError):
    pass


class ModelCacheStorageUnknown(_CacheUnknown, ModelCacheStorageError):
    default_reason = WaitReason.OBSERVATION_UNAVAILABLE


class ModelCacheRemovalOwnerInvalid(InvalidRequestError, ArtifactLifecycleError):
    """A removal owner that does not resolve or is not valid for the removal."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        reason: InvalidRequestReason | None = InvalidRequestReason.NOT_FOUND,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason
        self.typed_field = None


class ModelCacheDeletionFenceLost(SecurityRefusalError, ArtifactLifecycleError):
    """The destructive-effect fence of an artifact was lost: the removal stops."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = None


class ModelCacheCredentialPathUnsafe(SecurityRefusalError, RuntimeSecretError):
    """A credential path that is not a private regular file: never read."""


class _ArtifactWriterBusy(_CacheUnknown):
    """A dependency wait, not a failed transfer or consumed retry."""

    def __init__(self, digest: str) -> None:
        super().__init__(
            ModelCacheCode.OBJECT_BUSY,
            f"waiting for the managed-cache writer of object {digest}; resumes after that writer releases its lock",
            retry_after_seconds=_RETRY_BASE_SECONDS,
            recovery="resume",
        )


# Authorization failures wait for the configured credential to change; the
# worker then retries them automatically. Source-policy failures describe an
# untrusted or invalid pin and only a new request can change them. Every other
# failure, including an integrity mismatch whose bytes were discarded, retries
# with capped backoff while the operation remains the current intent.
_CREDENTIAL_FAILURE_CODES = frozenset(
    {
        ModelCacheCode.CREDENTIALS_MISSING,
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value,
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
    }
)
_CREDENTIAL_FAILURE_PUBLIC_CODES = frozenset(
    {"access_required", "access_denied", "credentials_invalid"}
)
_TERMINAL_FAILURE_CODES = _CREDENTIAL_FAILURE_CODES | frozenset(
    {
        SecurityRefusalReason.MODEL_CACHE_SOURCE_ACCESS_DENIED.value,
        ModelCacheCode.SOURCE_GONE,
        ModelCacheCode.SOURCE_INVALID,
        ModelCacheCode.SOURCE_UNSUPPORTED,
        ModelCacheCode.SOURCE_UNTRUSTED,
        ModelCacheCode.REDIRECT_FORBIDDEN,
        ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
    }
)


def model_cache_failure_is_terminal(code: object) -> bool:
    """Whether a typed model-cache failure waits for a changed credential or request."""

    return code in _TERMINAL_FAILURE_CODES or (
        isinstance(code, str) and is_security_failure(code)
    )


def _retryable_failure(error: BaseException) -> bool:
    """Classify by typed code: only authorization and source policy are terminal."""

    return not model_cache_failure_is_terminal(getattr(error, "code", None))


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
class ArtifactPart:
    """One source-published piece of a file the source hosts only split.

    Hugging Face caps a file at 50 GB, so a larger file exists there only as
    ``name.part00``, ``name.part01``, ...; the pieces are joined by byte
    concatenation, in order, into the one file the artifact names.
    """

    path: str
    source: str
    sha256: str
    expected_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One exact downloadable artifact in a resolved set.

    ``sha256`` and ``expected_bytes`` always describe the whole installed file.
    ``parts`` is set only when the source publishes the file split; the parts
    are a transport detail of one ingest, never cache objects of their own, so
    reuse (``cache_identity``) is by the whole file's bytes alone.
    """

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
    parts: tuple[ArtifactPart, ...] | None = None
    # Set only on the transient per-part view of a split file (``part_spec``):
    # transfer progress of a part is accounted on the whole file's ledger entry,
    # offset by the bytes of the parts already appended.
    ledger_sha256: str | None = None
    ledger_base: int = 0

    def part_spec(self, index: int) -> ArtifactSpec:
        """The transient single-file view that downloads part ``index``."""

        assert self.parts is not None
        part = self.parts[index]
        return replace(
            self,
            path=part.path,
            source=part.source,
            sha256=part.sha256,
            expected_bytes=part.expected_bytes,
            parts=None,
            ledger_sha256=self.sha256,
            ledger_base=sum(item.expected_bytes for item in self.parts[:index]),
        )

    def contract(self) -> CacheManifestArtifact:
        return CacheManifestArtifact(
            key=self.key,
            id=self.artifact_id,
            path=self.path,
            kind=self.kind,
            repository=self.repository,
            source=self.source,
            revision=self.revision,
            sha256=self.sha256,
            download_bytes=self.expected_bytes,
            roles=list(self.roles),
            model_content_sha256=self.model_content_sha256,
            parts=None
            if self.parts is None
            else [
                CacheManifestArtifactPart(
                    path=part.path,
                    source=part.source,
                    sha256=part.sha256,
                    download_bytes=part.expected_bytes,
                )
                for part in self.parts
            ],
        )

    def cache_identity(self) -> CacheIdentityArtifact:
        """Return only immutable bytes/source identity for cache reuse.

        Model/recipe content digests, roles, and mount selectors are
        provenance or runtime execution facts.  They must remain visible in
        the manifest but cannot make the same selected file bytes download a
        second time.
        """
        return CacheIdentityArtifact(
            key=self.key,
            id=self.artifact_id,
            path=self.path,
            kind=self.kind,
            repository=self.repository,
            source=self.source,
            revision=self.revision,
            sha256=self.sha256,
            download_bytes=self.expected_bytes,
        )

    @classmethod
    def from_manifest(cls, value: Mapping[str, object]) -> ArtifactSpec:
        try:
            wire = CacheManifestArtifact.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MANIFEST_INVALID, "cache manifest artifact is invalid"
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
            parts=(
                None
                if wire.parts is None
                else tuple(
                    ArtifactPart(
                        path=part.path,
                        source=part.source,
                        sha256=part.sha256,
                        expected_bytes=part.download_bytes,
                    )
                    for part in wire.parts
                )
            ),
        )
        _validate_artifact(result)
        return result


class _GitHubReleaseAssetMetadata(BaseModel):
    """Required GitHub release asset fields; provider extensions stay allowed."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: StrictInt
    name: StrictStr
    size: StrictInt
    state: StrictStr
    digest: StrictStr | None = None


class _GitHubReleaseMetadata(BaseModel):
    """Required release fields used by cache verification."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: StrictInt
    assets: list[_GitHubReleaseAssetMetadata]


class _GitHubErrorMetadata(BaseModel):
    """Bounded GitHub error fields used only to recognize rate limiting."""

    model_config = ConfigDict(extra="allow", strict=True)

    message: StrictStr


@dataclass(frozen=True, slots=True)
class ModelCacheRemovalScope:
    """Exact SQL membership and unshared bytes for one accepted removal."""

    selected_sets: tuple[str, ...]
    memberships: tuple[tuple[str, tuple[str, ...]], ...]
    selected_objects: tuple[str, ...]
    delete_objects: tuple[str, ...]
    shared_memberships: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ArtifactSetManifest:
    model_content_sha256: str | None
    recipe_revision_sha256: str | None
    model_content_digests: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]
    model_definition_ref: ModelReference | None = None

    def contract(self) -> CacheManifest:
        return CacheManifest(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            model_content_sha256=self.model_content_sha256,
            recipe_revision_sha256=self.recipe_revision_sha256,
            model_definition_ref=self.model_definition_ref,
            model_content_digests=list(self.model_content_digests),
            artifacts=[item.contract() for item in self.artifacts],
        )

    def document(self) -> dict[str, object]:
        """The manifest as the JSON document stored and hashed."""

        return serialize_json_value(self.contract())

    @property
    def digest(self) -> str:
        return _sha256_json(serialize_json_value(self.identity()))

    def identity(self) -> CacheIdentity:
        """Return the reusable identity, separate from requested provenance."""
        return CacheIdentity(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            artifacts=[item.cache_identity() for item in self.artifacts],
        )

    @property
    def expected_bytes(self) -> int:
        return sum(
            value.expected_bytes
            for _digest, value in _unique_artifacts(self.artifacts).items()
        )

    @classmethod
    def from_document(cls, value: object) -> ArtifactSetManifest:
        failure: Exception | None = None
        wire: CacheManifest | None = None
        try:
            document = require_mapping(value, "cache manifest must be a JSON object")
            wire = CacheManifest.model_validate_json(canonical_message(document))
        except (TypeError, ValueError, ValidationError) as error:
            failure = error
        if wire is None or _has_python_tuple(value):
            code = (
                ModelCacheCode.SCHEMA_UNSUPPORTED
                if isinstance(failure, ValidationError)
                and any(
                    issue.get("loc") == ("schema_version",)
                    for issue in failure.errors()
                )
                else ModelCacheCode.MANIFEST_INVALID
            )
            detail = (
                "cache manifest schema is unsupported"
                if code == ModelCacheCode.SCHEMA_UNSUPPORTED
                else "cache manifest shape is invalid"
            )
            raise ModelCacheResolutionInvalid(code, detail) from failure
        return cls.from_contract(wire)

    @classmethod
    def from_contract(cls, wire: CacheManifest) -> ArtifactSetManifest:
        """Build the validated storage identity from the canonical manifest."""

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


def _has_python_tuple(value: object) -> bool:
    """Whether a manifest document holds a Python-only tuple, which would be
    normalized into a JSON array and so must be refused."""

    if isinstance(value, tuple):
        return True
    if isinstance(value, Mapping):
        return any(_has_python_tuple(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_python_tuple(item) for item in value)
    return False


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


def _read_operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | Damaged:
    """Read and normalize one persisted operation envelope.

    A damaged envelope is ``Damaged``: the contract of :func:`read_or_rebuild`,
    which every caller goes through (``_operation_payload``).
    """

    return _parse_operation_envelope(operation, operation.payload)


def _parse_operation_envelope(
    operation: ModelCacheOperation, envelope: object
) -> ModelCacheOperationPayload | Damaged:
    try:
        parsed = parse_model_cache_payload(operation.kind, envelope)
        if isinstance(
            parsed, ModelCacheRemovalPayload
        ) and operation.plan_digest != _model_removal_intent_digest(
            parsed, actor=operation.actor, request_key=operation.request_key
        ):
            return Damaged(
                "persisted model removal intent no longer matches its accepted plan"
            )
        if isinstance(parsed, ModelCacheDownloadPayload):
            manifest = _read_manifest_document(serialize_json_value(parsed.manifest))
            if isinstance(manifest, Damaged):
                return manifest
        return parsed
    except ValidationError:
        return Damaged("persisted cache operation payload is invalid")


def _read_manifest_document(document: object) -> ArtifactSetManifest | Damaged:
    """``ArtifactSetManifest.from_document`` for a *stored* document.

    The strict parser refuses a malformed manifest at ingress; a persisted copy
    that no longer parses is damaged state, which the caller reads through
    :func:`read_or_rebuild`: it is ``Damaged`` here.
    """

    try:
        return ArtifactSetManifest.from_document(document)
    except ModelCacheResolutionError as error:
        return Damaged(error.detail)


def _rebuild_operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | None:
    """Re-derive a damaged download/repair envelope from what is still readable.

    The set row (keyed by the operation's own ``artifact_set_sha256``) holds the
    manifest and the retry counters restart: both are evidence the cache owns.
    A removal's plan cannot be re-derived (it is the accepted destructive intent,
    checked against its digest), so only its retry counters are; a plan that does
    not read stays damaged and the caller retires the row.

    The stored document is the one place that is still raw: it did not validate,
    so there is no model yet to read it through.
    """

    raw = operation.payload
    if not isinstance(raw, Mapping):
        return None
    candidate = dict(raw)
    if not isinstance(candidate.get("retry"), Mapping):
        candidate["retry"] = ModelCacheRetry(
            automatic_attempts=1, operator_retries=0
        ).model_dump(mode="json")
    if operation.kind in {"download", "repair"}:
        session = object_session(operation)
        set_digest = operation.artifact_set_sha256
        row = (
            None
            if session is None or set_digest is None
            else session.get(ModelCacheSet, set_digest)
        )
        if row is not None:
            candidate["manifest"] = row.manifest
            candidate["artifact_set_sha256"] = set_digest
    # A result that does not read is re-derived from the operation's own columns
    # (a download's result names its set; a removal's its selected sets).
    if candidate.get("result") is not None:
        try:
            parse_model_cache_result(operation.kind, candidate["result"])
        except (TypeError, ValueError, ValidationError):
            derived = _derived_result(
                operation,
                selected=candidate.get("selected"),
                reclaimed=candidate.get("reclaimed_bytes"),
            )
            candidate["result"] = (
                None if derived is None else derived.model_dump(mode="json")
            )
    # A removal keeps its accepted plan (checked against its digest): only the
    # retry counters are bookkeeping that can be restarted.
    rebuilt = _parse_operation_envelope(operation, candidate)
    return None if isinstance(rebuilt, Damaged) else rebuilt


def _derived_result(
    operation: ModelCacheOperation,
    *,
    selected: object = None,
    reclaimed: object = None,
) -> ModelCacheOperationResult | None:
    """The result a succeeded operation's own evidence implies, else ``None``."""

    if operation.state != State.SUCCEEDED:
        return None
    if operation.kind in {"download", "repair"}:
        if operation.artifact_set_sha256 is None:
            return None
        return ModelCacheDownloadResult(
            schema_version=SCHEMA_VERSION,
            artifact_set_sha256=operation.artifact_set_sha256,
            coverage="complete",
        )
    return ModelCacheRemovalResult(
        schema_version=SCHEMA_VERSION,
        removed_entries=list(selected) if isinstance(selected, list) else [],
        reclaimed_bytes=reclaimed if type(reclaimed) is int and reclaimed >= 0 else 0,
        cancelled_operations=[],
    )


def _manifest_of(payload: ModelCacheDownloadPayload) -> ArtifactSetManifest:
    """The artifact-set manifest a download or repair payload carries."""

    return ArtifactSetManifest.from_contract(payload.manifest)


def _updated[P: ModelCacheOperationPayload](payload: P, **changes: object) -> P:
    """``payload`` with ``changes`` applied and checked against its own contract.

    ``model_copy`` skips validation; a checkpoint is only a checkpoint when it
    still reads back, so a change that breaks it raises ``ValidationError``.
    """

    return read_stored_model(
        type(payload),
        serialize_json_value(payload.model_copy(update=changes)),
        from_json=True,
    )


def _operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | Residue:
    """The operation's payload, rebuilt from evidence, else a typed residue."""

    return read_or_rebuild(
        kind="model-cache-operation",
        subject=operation.id,
        read=lambda: _read_operation_payload(operation),
        rebuild=lambda: _rebuild_operation_payload(operation),
    )


def _removal_checkpoint(
    payload: ModelCacheOperationPayload,
) -> ModelCacheRemovalPayload | Residue:
    """The removal checkpoint of a payload, or a residue when it is not one."""

    if isinstance(payload, ModelCacheRemovalPayload):
        return payload
    return retire_as_unknown(
        "model-removal-checkpoint",
        "",
        BookkeepingReason.PERSISTED_STATE_DAMAGED,
        "model removal payload is invalid",
    )


def _operation_removal(
    operation: ModelCacheOperation,
) -> ModelCacheRemovalPayload | Residue:
    """The removal checkpoint of a stored operation, or the residue of its damage."""

    payload = _operation_payload(operation)
    return payload if isinstance(payload, Residue) else _removal_checkpoint(payload)


def _operation_cancellation(
    operation: ModelCacheOperation,
) -> ModelCacheCancellation | None:
    """The accepted cancel request; an unreadable operation has none."""

    payload = _operation_payload(operation)
    return None if isinstance(payload, Residue) else payload.cancellation


def _fresh_progress(now: datetime) -> ModelCacheOperationProgress:
    return cache_progress(
        ModelCacheCounters(
            phase=cast(ModelCacheOperationPhase, State.QUEUED.value),
            completed_artifacts=0,
            total_artifacts=0,
            downloaded_bytes=0,
        ),
        previous=None,
        now=now,
    )


def _read_operation_progress(
    operation: ModelCacheOperation,
) -> ModelCacheOperationProgress | Damaged:
    try:
        return read_stored_model(
            ModelCacheOperationProgress,
            canonical_message(operation.progress),
            from_json=True,
        )
    except ValidationError:
        return Damaged("persisted cache operation progress is invalid")


def _operation_progress(
    operation: ModelCacheOperation, now: datetime | None = None
) -> ModelCacheOperationProgress:
    """The operation's progress; a damaged measurement restarts from zero.

    Progress is a derived measurement (the transfer ledger and the receipts are
    the evidence), so it is never a reason to stop: the next sample rebuilds it.
    """

    result = read_or_rebuild(
        kind="model-cache-progress",
        subject=operation.id,
        read=lambda: _read_operation_progress(operation),
        rebuild=lambda: _fresh_progress(now or datetime.now(UTC)),
    )
    if isinstance(result, Residue):  # pragma: no cover - a fresh sample always builds
        return _fresh_progress(now or datetime.now(UTC))
    return result


def _write_operation_payload(
    kind: str, value: ModelCacheOperationPayload
) -> ModelCacheOperationPayload:
    """Validate and normalize a newly assembled operation envelope."""

    try:
        parsed = parse_model_cache_payload(
            kind,
            serialize_json_value(
                value.model_copy(update={"blockers": _wait_blockers(value)})
            ),
        )
        if isinstance(parsed, ModelCacheDownloadPayload):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return parsed
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.PAYLOAD_INVALID,
            "cache operation payload is invalid",
        ) from error


def _wait_blockers(payload: ModelCacheOperationPayload) -> list[OperationBlocker]:
    """What the operation waits for, taken from the failure it will retry.

    A failure the Controller retries by itself (or resumes once a credential
    changes) is a wait, so its reason is stored as a blocker; a terminal failure
    or a running operation waits for nothing.
    """

    failure = payload.failure
    if failure is None:
        return []
    waiting = (
        failure.retryable
        or failure.retry_time is not None
        or failure.code in _CREDENTIAL_FAILURE_PUBLIC_CODES
    )
    if not waiting:
        return []
    return bound_blockers(
        [
            make_blocker(
                failure.code,
                failure.detail,
                severity="info"
                if failure.code == ModelCacheCode.OBJECT_BUSY
                else "warning",
            )
        ]
    )


def _store_operation_payload(
    operation: ModelCacheOperation, kind: str, payload: ModelCacheOperationPayload
) -> None:
    """Persist a payload and log one line when the reason it waits changes."""

    current = read_row_column(operation, "payload")
    before = [] if current is None or isinstance(current, Residue) else current.blockers
    stored = _write_operation_payload(kind, payload)
    after = stored.blockers
    if after and {(b.code, tuple(b.node_ids)) for b in before} != {
        (b.code, tuple(b.node_ids)) for b in after
    }:
        _LOGGER.log(
            logging.WARNING
            if any(item.severity == "error" for item in after)
            else logging.INFO,
            "model cache %s %s is waiting: %s",
            operation.kind,
            operation.id,
            "; ".join(f"{item.code}: {item.detail}" for item in after[:4]),
        )
    operation.payload = serialize_json_value(stored)


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
    #: What a queued or interrupted operation waits for, and when it retries.
    blockers: tuple[OperationBlocker, ...] = ()
    next_attempt_at: str | None = None


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
) -> AvailabilityOperationFailure:
    """Translate an exception once, then persist the canonical public contract."""
    semantic_codes = {
        ModelCacheCode.CREDENTIALS_MISSING: "access_required",
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value: "access_denied",
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value: "credentials_invalid",
        ModelCacheCode.RATE_LIMITED: "rate_limited",
        ModelCacheCode.DIGEST_MISMATCH: "integrity_mismatch",
        ModelCacheCode.SOURCE_SIZE_MISMATCH: "integrity_mismatch",
        ModelCacheCode.CAPACITY: "capacity",
        ModelCacheCode.INTERRUPTED: "interrupted",
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
    return read_stored_model(
        AvailabilityOperationFailure,
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
        },
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_digest(value: object) -> bool:
    """Whether ``value`` is a lowercase hex digest (no raise: a damaged one is skipped)."""

    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and value == value.lower()
        and _is_hex(value)
    )


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
        ) from error
    if value != value.lower():
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
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
        or value.kind
        not in {
            "huggingface.file",
            "github-release.asset",
            "http.file",
            "file",
        }
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
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "cache artifact identity is invalid"
        )
    if value.expected_bytes == 0 and any(
        role.lower() in _WEIGHT_ROLES for role in value.roles
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID,
            "only verified empty support artifacts may have zero bytes",
        )
    if value.kind != "file" and value.revision is None:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.REVISION_MISSING,
            "remote cache artifacts require an immutable revision",
        )
    _validate_source(value.source)
    if value.kind == "github-release.asset":
        _github_release_asset_binding(value)
    if value.parts is not None:
        _validate_parts(value)


def _validate_parts(value: ArtifactSpec) -> None:
    parts = value.parts
    assert parts is not None
    if (
        value.kind != "huggingface.file"
        or not 2 <= len(parts) <= _MAX_ARTIFACT_PARTS
        or len({part.path for part in parts}) != len(parts)
        or value.path in {part.path for part in parts}
        or sum(part.expected_bytes for part in parts) != value.expected_bytes
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "cache artifact parts are invalid"
        )
    for part in parts:
        if (
            not _valid_relative_path(part.path)
            or len(part.sha256) != _DIGEST_LENGTH
            or part.sha256 != part.sha256.lower()
            or not _is_hex(part.sha256)
            or not isinstance(part.expected_bytes, int)
            or isinstance(part.expected_bytes, bool)
            or part.expected_bytes < 1
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID, "cache artifact part is invalid"
            )
        _validate_source(part.source)


def _validate_manifest(value: ArtifactSetManifest) -> None:
    if len(value.artifacts) < 1 or len(value.artifacts) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.ARTIFACT_COUNT, "cache artifact set count is invalid"
        )
    keys = [item.key for item in value.artifacts]
    if len(keys) != len(set(keys)):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.ARTIFACT_DUPLICATE, "cache artifact keys must be unique"
        )
    _unique_artifacts(value.artifacts)
    if any(
        not isinstance(item, str) or not _is_hex(item)
        for item in value.model_content_digests
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.MODEL_CONTENT_DIGESTS_INVALID,
            "cache model dependency pins are invalid",
        )
    encoded = json.dumps(
        value.document(), sort_keys=True, separators=(",", ":")
    ).encode()
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.MANIFEST_TOO_LARGE, "cache manifest exceeds the size limit"
        )


def _part_from_input(raw: object) -> ArtifactPart:
    value = require_mapping(raw, "artifact part")
    return ArtifactPart(
        path=str(value["path"]),
        source=str(value["source"]),
        sha256=str(value["sha256"]),
        expected_bytes=require_integer(value["download_bytes"], "part download bytes"),
    )


def _split_transient_bytes(
    manifest: ArtifactSetManifest, cached: frozenset[str] | None
) -> int:
    """Extra disk a split file needs beyond its own bytes while it is assembled."""

    return max(
        (
            max(part.expected_bytes for part in spec.parts)
            for digest, spec in _unique_artifacts(manifest.artifacts).items()
            if spec.parts is not None and (cached is None or digest not in cached)
        ),
        default=0,
    )


def _unique_artifacts(values: Sequence[ArtifactSpec]) -> dict[str, ArtifactSpec]:
    result: dict[str, ArtifactSpec] = {}
    for item in values:
        existing = result.get(item.sha256)
        if existing is not None and existing.expected_bytes != item.expected_bytes:
            raise ModelCacheResolutionRefused(
                ModelCacheCode.DIGEST_SIZE_CONFLICT,
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
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            )
        return
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID, "cache file source is invalid"
            )
        return
    raise ModelCacheResolutionRefused(
        ModelCacheCode.SOURCE_INVALID, "cache source must use HTTPS, HTTP or file"
    )


def _source_for_catalog_artifact(
    artifact: Mapping[str, object],
) -> tuple[str, str | None]:
    kind = artifact.get("kind")
    repository = artifact.get("repository")
    path = artifact.get("path")
    revision = artifact.get("revision")
    if not isinstance(repository, str) or not repository:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog artifact repository is invalid"
        )
    if not isinstance(path, str) or not path:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "catalog artifact path is invalid"
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID,
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
                raise ModelCacheResolutionRefused(
                    ModelCacheCode.SOURCE_INVALID,
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID,
                "catalog Hugging Face repository is invalid",
            )
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", revision
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.REVISION_INVALID,
                "catalog artifact revision is not immutable",
            )
        source = (
            f"https://huggingface.co/{repository_path}/resolve/{revision}/"
            f"{quote(path, safe='/')}"
        )
    elif kind == "http.file":
        source = repository
    elif kind == "github-release.asset":
        release_id = artifact.get("release_id")
        asset_id = artifact.get("asset_id")
        owner_and_name = _github_repository_parts(repository)
        if (
            type(release_id) is not int
            or release_id < 1
            or type(asset_id) is not int
            or asset_id < 1
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID,
                "catalog GitHub release asset identity is invalid",
            )
        owner, name = owner_and_name
        revision = f"github-release:{release_id}"
        source = f"https://{_GITHUB_API_HOST}/repos/{owner}/{name}/releases/assets/{asset_id}"
    else:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_UNSUPPORTED,
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


def _github_repository_parts(repository: str) -> tuple[str, str]:
    """Validate and split the canonical HTTPS GitHub repository URL."""

    try:
        # Reuse the current published contract's repository validator instead
        # of keeping a second URL policy for persisted artifact locators.
        GitHubReleaseSource.canonical_github_repository(repository)
        parsed = urlsplit(repository)
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        ) from error
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or parsed.path != f"/{parts[0]}/{parts[1]}":
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        )
    return parts[0], parts[1]


def _github_release_asset_binding(spec: ArtifactSpec) -> tuple[int, int, str, str]:
    """Validate that a persisted GitHub source binds one exact release asset."""

    if spec.kind != "github-release.asset" or not isinstance(spec.repository, str):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset identity is invalid"
        )
    owner, name = _github_repository_parts(spec.repository)
    revision_match = (
        re.fullmatch(r"github-release:([1-9][0-9]*)", spec.revision)
        if isinstance(spec.revision, str)
        else None
    )
    try:
        parsed = urlsplit(spec.source)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        ) from error
    expected_prefix = f"/repos/{owner}/{name}/releases/assets/"
    asset_id_text = parsed.path.removeprefix(expected_prefix)
    if (
        revision_match is None
        or parsed.scheme != "https"
        or parsed.hostname != _GITHUB_API_HOST
        or parsed.netloc != _GITHUB_API_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
        or re.fullmatch(r"[1-9][0-9]*", asset_id_text) is None
        or spec.source != f"https://{_GITHUB_API_HOST}{expected_prefix}{asset_id_text}"
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        )
    return int(revision_match.group(1)), int(asset_id_text), owner, name


def _datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso_now(value: datetime) -> str:
    """The ISO-8601 UTC spelling of a point in time that is always present."""

    return _datetime(value).astimezone(UTC).isoformat()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _datetime(value).astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    try:
        return _datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError) as error:
        raise ModelCacheConflictInvalid(
            ModelCacheCode.CURSOR_INVALID, "cache cursor boundary is invalid"
        ) from error


def _recipe_definition(
    document: Mapping[str, object] | RecipeDefinition,
) -> RecipeDefinition:
    if isinstance(document, RecipeDefinition):
        return document
    try:
        return read_recipe(document)
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.RECIPE_INVALID, "canonical recipe definition is invalid"
        ) from error


def _recipe_model_content_digests(
    document: Mapping[str, object] | RecipeDefinition,
) -> list[str]:
    recipe = _recipe_definition(document)
    raw_models = recipe.models
    if not raw_models:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.RECIPE_MODEL_MISSING,
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
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_PIN_INVALID,
                "canonical recipe model pin is invalid",
            )
        if digest not in result:
            result.append(digest)
    if len(result) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.DEPENDENCY_COUNT, "recipe model set is too large"
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
            raise InvalidType("catalog revision is not a model")
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.MODEL_DEFINITION_INVALID,
            "canonical model definition is invalid",
        ) from error
    if isinstance(definition.source, GitHubReleaseSource):
        repository = definition.source.repository
        revision = f"github-release:{definition.source.release_id}"
        release_id = definition.source.release_id
        assets = {asset.file_id: asset.asset_id for asset in definition.source.assets}
        provider_kind = "github-release.asset"
    else:
        repository = definition.source.repository
        revision = definition.source.revision
        release_id = None
        assets = {}
        provider_kind = "huggingface.file"
    result: list[dict[str, object]] = []
    for value in definition.files:
        artifact = {
            "id": value.id,
            "path": value.path,
            "kind": provider_kind,
            "repository": repository,
            "revision": revision,
            "sha256": value.sha256,
            "download_bytes": value.size_bytes,
            "roles": list(value.roles),
        }
        if release_id is not None:
            artifact["release_id"] = release_id
            artifact["asset_id"] = assets[value.id]
        if value.parts is not None:
            artifact["parts"] = [
                {
                    "path": part.path,
                    "sha256": part.sha256,
                    "download_bytes": part.size_bytes,
                }
                for part in value.parts
            ]
        result.append(artifact)
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

    model = document if isinstance(document, ModelDefinition) else read_model(document)
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


def _revision_identity(row: CatalogDocumentRevision | None) -> dict[str, object] | None:
    if row is None or not isinstance(row.content_digest, str):
        return None
    return ModelReference(
        publisher=row.publisher,
        slug=row.slug,
        content_sha256=row.content_digest,
    ).model_dump(mode="json")


@dataclass(slots=True)
class _BackgroundTransfer:
    """One download or repair this process is transferring on the shared pool."""

    set_digest: str
    manifest: ArtifactSetManifest
    force: bool
    specs: list[ArtifactSpec]
    planned_total: int
    transfer_attempt: int
    next_index: int = 0
    futures: list[Future[None]] = field(default_factory=list)
    future_specs: dict[Future[None], str] = field(default_factory=dict)
    failure: BaseException | None = None
    failure_artifact_key: str | None = None

    def pending(self) -> int:
        """Transfers submitted to the pool that have not finished."""

        return sum(1 for future in self.futures if not future.done())


class ModelCacheService:
    """Resolve, download, verify, repair and remove NAS model artifacts."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        root: Path,
        *,
        reserve_bytes: int = 10 * 1024**3,
        max_parallel_downloads: int = _DEFAULT_MAX_PARALLEL_DOWNLOADS,
        max_download_streams: int = _DEFAULT_MAX_DOWNLOAD_STREAMS,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx2.Client | None = None,
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
            raise InvalidValue("model cache root must be an absolute normalized path")
        if root.is_symlink():
            raise InvalidValue("model cache root must not be a symlink")
        if (
            not isinstance(reserve_bytes, int)
            or isinstance(reserve_bytes, bool)
            or reserve_bytes < 0
        ):
            raise InvalidValue("model cache reserve must be a non-negative integer")
        if (
            not isinstance(max_parallel_downloads, int)
            or isinstance(max_parallel_downloads, bool)
            or not 1 <= max_parallel_downloads <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise InvalidValue(
                "model cache parallel downloads must be between 1 and 32"
            )
        if (
            not isinstance(max_download_streams, int)
            or isinstance(max_download_streams, bool)
            or not 1 <= max_download_streams <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise InvalidValue("model cache download streams must be between 1 and 32")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for child in ("objects", "partials", "quarantine", "manifests", "locks"):
            directory = root / child
            if directory.is_symlink():
                raise InvalidValue(
                    "model cache storage directory must not be a symlink"
                )
            directory.mkdir(mode=0o750, exist_ok=True)
        self._sessions = sessions
        self._root = root
        self._reserve_bytes = reserve_bytes
        self._storage_demands: StorageDemands | None = None
        self._max_parallel_downloads = max_parallel_downloads
        self._streams = StreamGovernor(max_download_streams)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._removal_gate_after: tuple[str, str] | None = None
        self._removal_request_after: str | None = None
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
        self._background_operations: dict[str, _BackgroundTransfer] = {}
        self._active_digests: set[str] = set()
        self._hf_cooldown_until: datetime | None = None
        self._observed_credential_fingerprint: str | None = None
        self._progress_checkpoint_at: dict[str, datetime] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._range_reserved_bytes = 0
        self._reverified_at: dict[str, datetime] = {}
        # The lifecycle core decides every recovery outcome of an operation; this
        # adapter is the only writer of its stored ``state`` (see the module
        # docstring of ``lifecycle/model_cache.py``).
        self._lifecycle = ModelCacheAdapter(
            self._session,
            clock=lambda: self._clock(),
            effects=self,
            read_payload=_read_operation_payload,
            store_payload=lambda operation, payload: _store_operation_payload(
                operation, operation.kind, payload
            ),
            on_end=self._project_end,
        )
        self._reconciler = Reconciler(
            ModelCacheStore(self._session, self._lifecycle, self),
            {kind: self._lifecycle for kind in ("download", "repair", "remove")},
            clock=lambda: self._clock(),
            enabled=True,
        )

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

    # ---------------------------------------------- lifecycle core: what it asks

    def running_here(self) -> frozenset[str]:
        """Operations this process is transferring right now (``CacheEffects``)."""

        return frozenset(list(self._background_operations))

    def objects_present(self, payload: ModelCacheOperationPayload) -> bool | None:
        """Whether every object of the operation's set has a storage receipt."""

        if not isinstance(payload, ModelCacheDownloadPayload):
            return None
        try:
            return self._objects_present(_manifest_of(payload))
        except (TypeError, ValueError, OSError, ModelCacheError):
            return None

    def _objects_present(self, manifest: ArtifactSetManifest) -> bool:
        return self._managed_cached_objects(manifest) == frozenset(
            spec.sha256 for spec in manifest.artifacts
        )

    def set_is_cached(self, set_digest: str | None) -> bool:
        if set_digest is None:
            return False
        with self._session() as session:
            row = session.get(ModelCacheSet, set_digest)
            return row is not None and row.state == "cached"

    def effects_settled(
        self, operation_id: str, payload: ModelCacheOperationPayload | None
    ) -> bool:
        """Signal the operation's transfers to stop; whether none is still writing."""

        self._transfer_stop(operation_id).set()
        if not isinstance(payload, ModelCacheDownloadPayload):
            return False  # unreadable: a writer may still be active
        try:
            manifest = _manifest_of(payload)
        except (TypeError, ValueError):
            return False
        return self._artifact_effects_settled(operation_id, manifest)

    def cooldown_until(self, payload: ModelCacheOperationPayload) -> datetime | None:
        """The provider cooldown (rate limit) the operation's sources are under."""

        until = self._hf_cooldown_until
        if until is None or until <= self._clock():
            return None
        try:
            return until if self._payload_has_huggingface_source(payload) else None
        except (KeyError, TypeError, ValueError):
            return None

    # ----------------------------------------- damaged bookkeeping (rule 5)

    def _payload_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheOperationPayload | None:
        """The operation's payload, rebuilt from evidence; ``None`` when nothing can.

        Read-only: a caller that holds no write lock on the row skips it and
        carries on.  The worker paths use :meth:`_payload_or_retire`, which also
        retires the row so the damage is dealt with once.
        """

        value = _operation_payload(operation)
        return None if isinstance(value, Residue) else value

    def _payload_or_retire(
        self, operation: ModelCacheOperation, *, now: datetime | None = None
    ) -> ModelCacheOperationPayload | None:
        """As :meth:`_payload_or_none`, and end an unreadable operation as failed.

        The caller holds the row's write lock.  The damaged document is kept for
        inspection (``fail_corrupt`` records the end through the lifecycle core)
        and the caller skips the row: one damaged operation never stops the rest.
        """

        value = _operation_payload(operation)
        if not isinstance(value, Residue):
            return value
        self._retire_unreadable(operation, value, now=now)
        return None

    def _removal_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheRemovalPayload | None:
        """:meth:`_payload_or_none` for a removal; ``None`` for any other kind."""

        value = self._payload_or_none(operation)
        return value if isinstance(value, ModelCacheRemovalPayload) else None

    def _transfer_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheDownloadPayload | None:
        """:meth:`_payload_or_none` for a download or repair (its manifest, ledger)."""

        value = self._payload_or_none(operation)
        return value if isinstance(value, ModelCacheDownloadPayload) else None

    def _transfer_or_retire(
        self, operation: ModelCacheOperation, *, now: datetime | None = None
    ) -> ModelCacheDownloadPayload | None:
        """:meth:`_payload_or_retire` for a download or repair."""

        value = self._payload_or_retire(operation, now=now)
        return value if isinstance(value, ModelCacheDownloadPayload) else None

    def _retire_unreadable(
        self,
        operation: ModelCacheOperation,
        residue: Residue,
        *,
        now: datetime | None = None,
    ) -> None:
        at = now or self._clock()
        if not self._lifecycle.lifecycle(operation, at).terminal:
            self._lifecycle.fail_corrupt(
                operation, f"{residue.reason.value}: {residue.note}", at
            )
        log_event(
            _LOGGER,
            ModelCacheCode.OPERATION_UNREADABLE,
            service="controller",
            operation_id=operation.id,
            code=residue.reason.value,
            detail=residue.note[:200],
        )

    def _store_failure(
        self, operation: ModelCacheOperation, failure: AvailabilityOperationFailure
    ) -> ModelCacheOperationPayload | None:
        """Record ``failure`` in the operation's document; ``None`` when unreadable.

        The lifecycle core already holds the outcome (state, retry clock); the
        document only carries the explanation, so an unreadable one is skipped.
        """

        current = self._payload_or_none(operation)
        if current is None:
            return None
        payload = current.model_copy(update={"failure": failure})
        _store_operation_payload(operation, operation.kind, payload)
        return payload

    def _stored_manifest(self, row: ModelCacheSet) -> ArtifactSetManifest | None:
        """The manifest a set row stores, re-derived from the catalog when damaged.

        The evidence is the row's own pins (model content, recipe revision): the
        catalog resolves them to a manifest, which counts only when it names the
        same artifact set.  ``None`` (a typed unknown) when nothing re-derives it:
        the caller skips this set; the next request or sweep reconciles it.
        """

        def rebuild() -> ArtifactSetManifest | None:
            try:
                candidate = self.resolve_artifact_set(
                    model_content_sha256=row.model_content_sha256,
                    recipe_revision_sha256=row.recipe_revision_sha256,
                )
            except ModelCacheError:
                return None
            return candidate if candidate.digest == row.artifact_set_sha256 else None

        value = read_or_rebuild(
            kind="model-cache-set",
            subject=row.artifact_set_sha256,
            read=lambda: _read_manifest_document(row.manifest),
            rebuild=rebuild,
            reason=BookkeepingReason.PERSISTED_STATE_DAMAGED,
        )
        return None if isinstance(value, Residue) else value

    def _project_end(
        self, operation: ModelCacheOperation, before: Lifecycle, after: Lifecycle
    ) -> None:
        """What a cancelled download leaves behind, in the transaction that ends it.

        The set's row follows what storage proves (cached when every object has a
        receipt, else incomplete) unless a sibling operation still works on it.
        """

        if after.state is not State.CANCELLED or operation.kind != "download":
            return
        set_digest = operation.artifact_set_sha256
        now = self._clock()
        payload = _operation_payload(operation)
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "completed", now)
        )
        operation.current_artifact_key = None
        if isinstance(payload, Residue):
            # The cancel stands; the set row is left to the storage sweep, which
            # follows what the receipts prove.
            return
        payload = payload.model_copy(update={"failure": None})
        _store_operation_payload(operation, operation.kind, payload)
        if set_digest is None or not isinstance(payload, ModelCacheDownloadPayload):
            return
        try:
            manifest = _manifest_of(payload)
        except ValueError:
            return
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
        # The operation's own transaction: the set projection commits with it.
        session = object_session(operation)
        assert session is not None
        self._project_cancelled_set(
            session,
            set_digest,
            exclude_operation_id=operation.id,
            verified_bytes=verified_bytes,
            coverage_complete=coverage_complete,
            now=now,
        )

    @staticmethod
    def _project_cancelled_set(
        session: Session,
        set_digest: str,
        *,
        exclude_operation_id: str,
        verified_bytes: int,
        coverage_complete: bool,
        now: datetime,
    ) -> None:
        sibling_work = session.scalar(
            select(func.count())
            .select_from(ModelCacheOperation)
            .where(
                ModelCacheOperation.artifact_set_sha256 == set_digest,
                ModelCacheOperation.id != exclude_operation_id,
                ModelCacheOperation.kind.in_(["download", "repair"]),
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.RECIPE_IDENTITY_AMBIGUOUS,
                "recipe revision digest and ID cannot both be supplied",
            )
        if artifacts is not None:
            if not self._fixture_sources:
                raise ModelCacheResolutionRefused(
                    ModelCacheCode.FIXTURE_SOURCES_FORBIDDEN,
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
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.PIN_REQUIRED,
                "an exact model definition or recipe revision is required",
            )
        with self._session() as session:
            recipe_document: RecipeDefinition | None = None
            recipe_model_digests: list[str] = []
            if recipe_digest is not None or recipe_revision_id is not None:
                recipe_document, _resolved_recipe_id, resolved_recipe_digest = (
                    self._recipe_document(
                        session, recipe_digest, recipe_revision_id, tolerant=True
                    )
                )
                if (
                    recipe_digest is not None
                    and resolved_recipe_digest != recipe_digest
                ):
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
                        "exact recipe revision is not resolved",
                    )
                recipe_digest = resolved_recipe_digest
                recipe_model_digests = _recipe_model_content_digests(recipe_document)
                if not recipe_model_digests:
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_MODEL_MISSING,
                        "recipe does not bind an exact model definition",
                    )
                if (
                    model_digest is not None
                    and model_digest not in recipe_model_digests
                ):
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.PIN_MISMATCH,
                        "recipe and requested model definitions do not match",
                    )
                model_digest = model_digest or recipe_model_digests[0]
            if model_digest is None:
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.PIN_REQUIRED,
                    "an exact model definition is required after recipe resolution",
                )
            model_rows: dict[str, CatalogDocumentRevision] = {}
            aliases: dict[str, str] = {}
            requested_model_digests = (
                recipe_model_digests if recipe_document is not None else [model_digest]
            )
            for digest in requested_model_digests:
                self._collect_model_definitions(
                    session, digest, model_rows, aliases=aliases
                )
            # A model whose pinned revision is unreadable resolved to its newest
            # readable revision; the manifest names what it actually contains.
            model_digest = aliases.get(model_digest, model_digest)
            requested_by_actual = {actual: asked for asked, actual in aliases.items()}
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
                selected_ids = _recipe_model_file_ids(
                    recipe_document, requested_by_actual.get(digest, digest)
                )
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
        # A catalog row whose identity digest is damaged cannot be pinned: it is
        # skipped like any row the catalog does not hold, and the rest carry on.
        rows = [row for row in rows if _is_digest(row.content_digest)]
        if len(rows) != 1:
            if not rows:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.SELECTOR_MISSING, "model selector was not found"
                )
            raise ModelCacheConflictInvalid(
                ModelCacheCode.SELECTOR_AMBIGUOUS,
                "model selector matches multiple models",
            )
        return str(rows[0].content_digest)

    def resolve_latest_cached(
        self,
        *,
        recipe_identity: str,
        model_content_sha256: str | None = None,
        model_variant: str | None = None,
        exact_revision_id: str | None = None,
    ) -> CacheResolution:
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
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_IDENTITY_INVALID, "recipe identity is required"
            )
        identity = recipe_identity.strip().casefold()
        requested_model = _optional_digest(model_content_sha256)
        if model_variant is not None and (
            not isinstance(model_variant, str)
            or not 1 <= len(model_variant.strip()) <= 128
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_VARIANT_INVALID, "model variant is invalid"
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
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.RECIPE_IDENTITY_MISSING,
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
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_REVISION_INVALID,
                        "exact recipe revision is invalid",
                    )
                exact = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.id == exact_revision_id.strip(),
                    )
                )
                if exact is None or exact.document_id != seed.document_id:
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
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
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.RECIPE_REVISION_MISSING,
                    "recipe has no active revision",
                )
            if exact is not None:
                if exact.state != "active":
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_REVISION_MISSING,
                        "selected recipe revision is not active",
                    )
                selection_pool = [exact]
            else:
                selection_pool = revisions

            def compatible_model(
                revision: CatalogDocumentRevision,
            ) -> tuple[str, str | None]:
                try:
                    recipe = read_catalog_document(revision)
                except CatalogRevisionContractError:
                    # Written under another contract; not usable, not fatal.
                    return "", None
                if not isinstance(recipe, RecipeDefinition):
                    raise ModelCacheResolutionInvalid(
                        ModelCacheCode.RECIPE_INVALID,
                        "recipe revision is not canonical",
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
                    try:
                        model = read_catalog_document(model_revision)
                    except CatalogRevisionContractError:
                        continue
                    if not isinstance(model, ModelDefinition):
                        continue
                    variant = model.identity.variant
                    if requested_variant is None or variant == requested_variant:
                        return digest, variant
                return "", None

            def verified_image(revision_id: str) -> RevisionImage | None:
                # SQL names the images the recipe's builds produced; managed
                # storage owns whether an archive is present. The build row
                # carries the archive identity, so no receipt row joins them.
                if self._runtime_archive_available is None:
                    return None
                images = revision_images(session, [revision_id], same_source=True)
                for image in images.get(revision_id, ()):
                    if self._runtime_archive_available(
                        image.archive_sha256, image.image_bytes
                    ):
                        return image
                return None

            selected: (
                tuple[
                    CatalogDocumentRevision,
                    str,
                    str | None,
                    RevisionImage | None,
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
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.PIN_MISMATCH,
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
            # A set is keyed by its bytes and keeps the provenance of the
            # revision that cached it first: a later model revision with the
            # same files is cached under that row, as compilation sees it.
            try:
                shared = session.get(
                    ModelCacheSet,
                    self.resolve_artifact_set(recipe_revision_id=revision.id).digest,
                )
            except ModelCacheError:
                shared = None
            if (
                shared is not None
                and shared.state == "cached"
                and shared not in model_rows
            ):
                model_rows.append(shared)

            def model_set_available(row: ModelCacheSet) -> bool:
                manifest = self._stored_manifest(row)
                if manifest is None:
                    return False  # unknown: this set is skipped, not a blocker
                if manifest.digest != row.artifact_set_sha256:
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.MANIFEST_IDENTITY_MISMATCH,
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
            image_bytes = receipt.image_bytes if receipt is not None else None
            image_digest = receipt.image_digest if receipt is not None else None
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
                blockers.append(ModelCacheBlockerCode.RECIPE_NOT_CACHED)
            if not model_cached:
                blockers.append(ModelCacheBlockerCode.MODEL_NOT_CACHED)
            return CacheResolution(
                recipe=CachedRecipeResolution(
                    recipe_revision_id=revision.id,
                    document_id=revision.document_id,
                    publisher=revision.publisher,
                    slug=revision.slug,
                    revision_number=revision.revision_number,
                    content_sha256=revision.content_digest,
                    cached=recipe_cached,
                    cache_state="cached" if recipe_cached else "missing",
                    artifact_set_sha256=receipt.archive_sha256 if receipt else None,
                    expected_bytes=image_bytes,
                    verified_bytes=image_bytes,
                    image_digest=image_digest,
                    update_available=latest is not None,
                ),
                model=CachedModelResolution(
                    content_sha256=digest,
                    cached=model_cached,
                    cache_state="cached" if model_cached else "missing",
                    artifact_set_sha256=(
                        model_set.artifact_set_sha256 if model_set else None
                    ),
                    expected_bytes=model_expected,
                    verified_bytes=model_verified,
                    variant=variant,
                ),
                resources=CachedResourceEstimate(
                    additional_disk_bytes=additional,
                    model_bytes=model_expected,
                    image_bytes=image_bytes,
                ),
                blockers=blockers,
            )

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
        # A capacity blocker is a wait, not a refusal: start_download queues the
        # operation, which is claimed once the space it needs exists.
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
    ) -> CacheOperationView:
        """Accept a durable removal of the named model against current state.

        The removal applies to what the selector resolves to now. Sets still in
        use are fenced against new consumers and the removal waits for their
        current owners.
        """

        request_key = _request_key(request_key)
        normalized_selector = _model_selector(selector).casefold()
        with self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                return self._replay_model_removal(
                    existing, actor=actor, selector=normalized_selector
                )

        reviewed = self.review_model_removal(normalized_selector)
        blockers = refusing_removal_blockers(reviewed)
        if blockers:
            first = blockers[0]
            raise ModelCacheConflictRefused(
                first.code,
                first.detail,
                recovery="retry" if first.retryable else None,
            )

        try:
            with self._lock, self._session(write=True) as session:
                existing = session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.request_key == request_key
                    )
                )
                if existing is not None:
                    return self._replay_model_removal(
                        existing, actor=actor, selector=normalized_selector
                    )
                digest = self._resolve_model_selector_in_session(
                    session, normalized_selector
                )
                operation = self._accept_model_removal(
                    session,
                    actor=actor,
                    request_key=request_key,
                    selector=normalized_selector,
                    model_content_sha256=digest,
                    selected_sets=None,
                    review_digest=reviewed.review_digest,
                )
                operation_id = operation.id
                removal = _operation_removal(operation)
                superseded = self._cancel_superseded_downloads(
                    session,
                    () if isinstance(removal, Residue) else removal.selected,
                    actor=actor,
                    request_key=request_key,
                )
        except IntegrityError:
            # The unique request key arbitrates first submission across
            # Controller processes.  Resolve the winner only after rollback.
            replay = self._model_removal_by_request(
                request_key, actor=actor, selector=normalized_selector
            )
            if replay is None:
                raise
            return replay
        for superseded_id in superseded:
            self.signal_cancelled_operation(superseded_id)
        return self.get_operation(operation_id)

    def accept_unused_removal(
        self,
        model_content_sha256: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> CacheOperationView:
        """Accept the removal of a model nobody asked to remove.

        The same durable, fenced removal as a request-led one, with one
        difference: ``verify`` is called with every set and object gate held,
        before the removal is accepted, and refuses it by raising. A sweep
        therefore never fences a model that a load reached after it looked, and
        never leaves a fence behind that waits for a current owner.
        """

        request_key = _request_key(request_key)
        with self._lock, self._session(write=True) as session:
            operation = self._accept_model_removal(
                session,
                actor=actor,
                request_key=request_key,
                selector=model_content_sha256,
                model_content_sha256=model_content_sha256,
                selected_sets=None,
                verify=verify,
            )
            operation_id = operation.id
        return self.get_operation(operation_id)

    def accept_unused_set_removal(
        self,
        set_digest: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> CacheOperationView:
        """Remove an abandoned exact set even if its catalog model is gone."""
        request_key = _request_key(request_key)
        with self._lock, self._session(write=True) as session:
            operation = self._accept_model_removal(
                session,
                actor=actor,
                request_key=request_key,
                selector=set_digest,
                model_content_sha256=None,
                selected_sets=(set_digest,),
                verify=verify,
            )
            operation_id = operation.id
        return self.get_operation(operation_id)

    def unused_set_bytes(self, set_digest: str) -> int | None:
        """Measured complete and partial bytes; unknown size defers eviction."""
        with self._session() as session:
            scope = self._model_removal_scope_for_sets(session, (set_digest,))
        assets = self.removal_asset_status(scope)
        available = [
            asset.available_bytes for asset in assets if asset.kind == "model-set"
        ]
        return (
            None
            if any(value is None for value in available)
            else sum(value for value in available if value is not None)
        )

    def _cancel_superseded_downloads(
        self,
        session: Session,
        selected_sets: Sequence[str],
        *,
        actor: str,
        request_key: str,
    ) -> tuple[str, ...]:
        """The newer removal intent supersedes older downloads of its sets."""

        if not selected_sets:
            return ()
        cancelled: list[str] = []
        for operation_id in session.scalars(
            select(ModelCacheOperation.id)
            .where(
                ModelCacheOperation.kind == "download",
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
                ModelCacheOperation.artifact_set_sha256.in_(list(selected_sets)),
            )
            .order_by(ModelCacheOperation.id)
        ):
            if self.cancel_operation_in_session(
                session,
                operation_id,
                actor=actor,
                request_key=str(uuid.uuid5(uuid.UUID(request_key), operation_id)),
                reason="superseded by a newer model removal request",
            ):
                cancelled.append(operation_id)
        return tuple(cancelled)

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
                            code=ModelCacheCode.REMOVAL_REFERENCED,
                            detail=entry.reason,
                            retryable=False,
                            recovery_actions=["resolve_reference"],
                        )
                    )
            findings.extend(active_removals)
            findings.extend(self.retained_model_object_findings(scope))
            blockers.extend(
                CacheRemovalBlocker(
                    code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
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
        owners: dict[str, tuple[ModelCacheOperation, ModelCacheRemovalPayload]] = {}
        for gate in selected_gates:
            owner_id = gate.removal_owner_id
            if (
                gate.removal_owner_kind != "model-cache-operation"
                or not isinstance(owner_id, str)
                or not owner_id
                or not isinstance(gate.removal_fence, str)
                or not gate.removal_fence
            ):
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                    "a selected cache identity has an unreadable removal owner; retry after the owner is reconciled",
                    retryable=True,
                )
            cached = owners.get(owner_id)
            if cached is None:
                operation = session.get(ModelCacheOperation, owner_id)
                if operation is None or operation.kind != "remove":
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                        "a selected cache identity has no readable removal operation owner",
                        retryable=True,
                    )
                payload = _operation_removal(operation)
                if isinstance(payload, Residue):
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                        "a selected cache identity has a malformed removal owner",
                        retryable=True,
                    )
                if payload.removal_fence != gate.removal_fence:
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                        "a selected cache identity removal fence disagrees with its owner",
                        retryable=True,
                    )
                cached = (operation, payload)
                owners[owner_id] = cached
            operation, payload = cached
            expected_target = (
                payload.selected
                if gate.artifact_kind == "model-set"
                else payload.delete_objects
            )
            if gate.artifact_sha256 not in expected_target:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                    "a selected cache identity is not covered by its stored removal plan",
                    retryable=True,
                )
            if operation.state not in model_cache_states.LIVE:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                    "a selected cache identity remains fenced by a non-active removal owner",
                    retryable=True,
                )
            try:
                identity = ArtifactIdentity(
                    kind=cast(ArtifactKind, gate.artifact_kind),
                    sha256=gate.artifact_sha256,
                )
            except ValueError as error:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
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
                    state=model_cache_states.adopted(operation.state),
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
            raise InvalidValue("model removal child requires an exact non-empty scope")
        if len(supplied_sets) != len(set(supplied_sets)):
            raise InvalidValue("model removal child scope contains duplicate sets")
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
        payload = _operation_removal(operation)
        if isinstance(payload, Residue) or not isinstance(operation.plan_digest, str):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REMOVAL_PLAN_INVALID,
                "model removal child has no readable immutable plan",
            )
        accepted_sets = tuple(payload.selected)
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
                manifest = None if row is None else self._stored_manifest(row)
                if manifest is None:
                    # Destructive guard: without a readable manifest the exact
                    # objects of the scope are unknown, so nothing is removed.
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REMOVAL_SCOPE_UNAVAILABLE,
                        f"selected model set {set_digest} is no longer available",
                    )
                specs = _unique_artifacts(manifest.artifacts)
                expected = {
                    digest: spec.expected_bytes for digest, spec in specs.items()
                }
                if set(expected) != set(memberships_by_set.get(set_digest, ())):
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REMOVAL_SCOPE_INVALID,
                        f"model set {set_digest} membership disagrees with its manifest",
                    )
                for digest, expected_bytes in expected.items():
                    previous = expected_by_object.setdefault(digest, expected_bytes)
                    if previous != expected_bytes:
                        raise ModelCacheStorageRefused(
                            ModelCacheCode.REMOVAL_SCOPE_INVALID,
                            f"model object {digest} has inconsistent expected lengths",
                        )
                expected_by_set[set_digest] = expected

        object_status: dict[str, tuple[AssetAvailability, int | None]] = {}
        for digest, expected_bytes in sorted(expected_by_object.items()):
            try:
                verified_bytes = self._stored_object(digest, expected_bytes)
            except OSError:
                object_status[digest] = (AssetAvailability.UNKNOWN, None)
                continue
            if verified_bytes is not None:
                object_status[digest] = (AssetAvailability.VERIFIED, verified_bytes)
                continue
            try:
                metadata = self._object_path(digest).lstat()
            except (FileNotFoundError, NotADirectoryError):
                object_status[digest] = (AssetAvailability.MISSING, 0)
                continue
            except OSError:
                object_status[digest] = (AssetAvailability.UNKNOWN, None)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                object_status[digest] = (AssetAvailability.UNKNOWN, None)
                continue
            observed_bytes = metadata.st_size
            if observed_bytes < expected_bytes:
                availability: AssetAvailability = AssetAvailability.PARTIAL
            else:
                # Includes an exact-size file without its verified receipt
                # and a file larger than the expected length. Neither is ready.
                availability = AssetAvailability.UNKNOWN
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
                    return AssetAvailability.UNKNOWN, None
                if available == 0:
                    return AssetAvailability.MISSING, 0
                return (
                    AssetAvailability.PARTIAL
                    if available < expected_bytes
                    else AssetAvailability.UNKNOWN,
                    available,
                )
            try:
                metadata = partial.lstat()
            except (FileNotFoundError, NotADirectoryError):
                return AssetAvailability.MISSING, 0
            except OSError:
                return AssetAvailability.UNKNOWN, None
            if not stat.S_ISREG(metadata.st_mode):
                return AssetAvailability.UNKNOWN, None
            observed = metadata.st_size
            if observed < expected_bytes:
                return AssetAvailability.PARTIAL, observed
            return AssetAvailability.UNKNOWN, observed

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
            if statuses and all(
                item[0] == AssetAvailability.VERIFIED for item in statuses
            ):
                availability: AssetAvailability = AssetAvailability.VERIFIED
            elif statuses and all(
                item[0] == AssetAvailability.MISSING for item in statuses
            ):
                availability = AssetAvailability.MISSING
            elif any(item[0] == AssetAvailability.UNKNOWN for item in statuses):
                availability = AssetAvailability.UNKNOWN
            elif statuses:
                availability = AssetAvailability.PARTIAL
            else:
                availability = AssetAvailability.UNKNOWN
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
                raise ModelCacheStorageRefused(
                    ModelCacheCode.REMOVAL_SCOPE_INVALID,
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
        self, request_key: str, *, actor: str, selector: str
    ) -> CacheOperationView | None:
        with self._session() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if operation is None:
                return None
            return self._replay_model_removal(operation, actor=actor, selector=selector)

    def _replay_model_removal(
        self, operation: ModelCacheOperation, *, actor: str, selector: str
    ) -> CacheOperationView:
        if operation.kind != "remove" or operation.actor != actor:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another cache operation",
            )
        # A replay names its operation by the request key; an unreadable payload
        # cannot contradict it, so the stored operation is what the caller gets.
        payload = self._payload_or_none(operation)
        if payload is not None and payload.selector != selector:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
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
        verify: Callable[[Session, tuple[str, ...]], None] | None = None,
    ) -> ModelCacheOperation:
        existing = session.scalar(
            select(ModelCacheOperation).where(
                ModelCacheOperation.request_key == request_key
            )
        )
        if existing is not None:
            self._replay_model_removal(existing, actor=actor, selector=selector)
            return existing

        if selected_sets is None:
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
                raise InvalidValue("model removal scope contains duplicate sets")
            selected = tuple(sorted(supplied))
        # Read and validate exact SQL membership before the ordered gate
        # acquisition, then re-read it after the fences are held.
        scope = self._model_removal_scope_for_sets(session, selected)
        if expected_scope is not None and scope != expected_scope:
            raise ModelCacheConflictRefused(
                ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
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
                    raise ModelCacheDeletionFenceLost(
                        ArtifactLifecycleCode.DELETION_FENCE_LOST,
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
                raise ModelCacheConflictRefused(
                    ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                    "model-set membership changed while removal ownership was reserved",
                )
            # Sets still in use do not refuse the request: the accepted fence
            # stops new consumers and each destructive step waits until the
            # current owners have released the set.
        except ArtifactLifecycleError as error:
            raise ModelCacheConflictRefused(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error
        if verify is not None:
            # An unattended removal re-proves the sets unused with every gate
            # held; raising rolls the reservation back with this transaction.
            verify(session, scope.selected_sets)

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
            raise ModelCacheConflictRefused(
                ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                "model object sharing changed while removal ownership was reserved",
            )
        plan = ModelCacheRemovalPayload(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            selector=selector,
            model_content_sha256=model_content_sha256,
            operator_action="remove-model",
            review_digest=review_digest,
            removal_fence=fence,
            selected=list(scope.selected_sets),
            selected_objects=list(scope.selected_objects),
            delete_objects=list(delete_objects),
            object_index=0,
            object_pending_bytes=None,
            reclaimed_bytes=0,
            set_index=0,
            retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
            result=None,
        )
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
        stored_plan = _write_operation_payload("remove", plan)
        removal_checkpoint = _removal_checkpoint(stored_plan)
        # The plan was written one statement ago, so it reads back.
        assert not isinstance(removal_checkpoint, Residue)
        operation = ModelCacheAdapter.new_operation(
            id=operation_id,
            request_key=request_key,
            schema_version=SCHEMA_VERSION,
            kind="remove",
            attempt=1,
            artifact_set_sha256=None,
            plan_digest=_model_removal_intent_digest(
                removal_checkpoint,
                actor=actor,
                request_key=request_key,
            ),
            payload=serialize_json_value(stored_plan),
            progress=progress_document(progress),
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
        previous: ModelCacheOperationProgress | None,
        now: datetime,
    ) -> ModelCacheOperationProgress:
        return cache_progress(
            ModelCacheCounters(
                phase=phase,
                completed_artifacts=completed_items,
                total_artifacts=total_items,
                downloaded_bytes=reclaimed_bytes,
                current_artifact_key=current_key,
            ),
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
                raise ModelCacheStorageRefused(
                    ModelCacheCode.LOCK_UNAVAILABLE,
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
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REMOVAL_PATH_UNSAFE,
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
                        raise ModelCacheStorageRefused(
                            ModelCacheCode.REMOVAL_PATH_UNSAFE,
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
                raise ModelCacheStorageRefused(
                    ModelCacheCode.REMOVAL_PATH_UNSAFE,
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
    ) -> ModelCacheRemovalPayload | None:
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
                or operation.state not in model_cache_states.LIVE
            ):
                return None
            payload = self._removal_or_none(operation)
            if payload is None or payload.removal_fence != fence:
                return None  # unreadable: the step's entry retires the row
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
                or operation.state not in model_cache_states.LIVE
            ):
                return False
            payload = self._removal_or_none(operation)
            if payload is None or payload.removal_fence != fence:
                return False  # unreadable: the step's entry retires the row
            current_index = payload.object_index if object_step else payload.set_index
            if current_index != expected_index:
                return False
            object_index = payload.object_index
            object_pending_bytes = payload.object_pending_bytes
            reclaimed_bytes = payload.reclaimed_bytes
            set_index = payload.set_index
            if object_step:
                if pending_bytes is None:
                    return False  # no byte checkpoint yet: the step measures again
                if complete_step:
                    if payload.object_pending_bytes != pending_bytes:
                        return False
                    object_index = expected_index + 1
                    object_pending_bytes = None
                    reclaimed_bytes += pending_bytes
                else:
                    object_pending_bytes = pending_bytes
            elif complete_step:
                set_index = expected_index + 1
            try:
                checkpoint = _updated(
                    payload,
                    retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                    failure=None,
                    object_index=object_index,
                    object_pending_bytes=object_pending_bytes,
                    reclaimed_bytes=reclaimed_bytes,
                    set_index=set_index,
                )
            except ValidationError:
                return False  # the step's entry retires the unreadable row
            previous = _operation_progress(operation)
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            total_items = len(checkpoint.delete_objects) + len(checkpoint.selected)
            completed_items = object_index + set_index
            current_key: str | None = None
            if object_index < len(checkpoint.delete_objects):
                current_key = f"object:{checkpoint.delete_objects[object_index]}"
            elif set_index < len(checkpoint.selected):
                current_key = f"set:{checkpoint.selected[set_index]}"
            operation.progress = progress_document(
                self._model_removal_progress(
                    phase="reclaiming",
                    total_items=total_items,
                    completed_items=completed_items,
                    reclaimed_bytes=checkpoint.reclaimed_bytes,
                    current_key=current_key,
                    previous=previous,
                    now=now,
                )
            )
            _store_operation_payload(operation, "remove", checkpoint)
            self._lifecycle.renew(
                operation, self._claim_owner, _TRANSFER_CLAIM_SECONDS, now
            )
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
                    ModelCacheOperation.state.in_(model_cache_states.LIVE),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return
            if self._payload_or_retire(operation, now=now) is None:
                return
            # The dependency's own hint is a floor; the core's bounded backoff
            # is the schedule (one policy for every kind, one clock).
            self._lifecycle.settle(
                operation,
                Reported(
                    Outcome.UNKNOWN,
                    retry_after=now + timedelta(seconds=retry_after_seconds),
                    reason=detail,
                ),
                now,
                interrupted=True,
            )
            next_retry = operation.next_action_at
            assert next_retry is not None
            delay = max(1, round((_aware(next_retry) - now).total_seconds()))
            checkpoint = self._removal_or_none(operation)
            if checkpoint is None:
                return
            artifact_key = (
                f"object:{checkpoint.delete_objects[checkpoint.object_index]}"
                if checkpoint.object_index < len(checkpoint.delete_objects)
                else f"set:{checkpoint.selected[checkpoint.set_index]}"
                if checkpoint.set_index < len(checkpoint.selected)
                else "removal-finalization"
            )
            checkpoint = checkpoint.model_copy(
                update={
                    "failure": _cache_failure(
                        ModelCacheCode.REMOVAL_WAIT,
                        "Automatic retry resumes this exact checkpoint when its "
                        "storage or ownership dependency clears. " + detail,
                        retryable=True,
                        recovery="inspect",
                        retry_time=_iso(next_retry),
                        retry_after_seconds=delay,
                        artifact_key=artifact_key,
                    )
                }
            )
            operation.progress = progress_document(
                cache_phase(
                    _operation_progress(operation), "reclaiming", now, waiting=True
                )
            )
            operation.last_error = redact_text(detail)[:512]
            _store_operation_payload(operation, "remove", checkpoint)

    def reconcile_requested_removals(self, *, limit: int = 64) -> int:
        """Accepted newer downloads fence older removers before transfer dispatch."""
        if isinstance(self._sessions, Session):
            return 0  # a borrowed SQL transaction cannot precede a storage lock
        with self._session() as session:
            accepted_requests = []
            observed_request = False
            for operation in session.scalars(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.kind.in_(("download", "repair")),
                    ModelCacheOperation.state.in_(model_cache_states.LIVE),
                )
                .where(
                    ModelCacheOperation.id > self._removal_request_after
                    if self._removal_request_after is not None
                    else true()
                )
                .order_by(ModelCacheOperation.id)
                .limit(limit)
            ):
                observed_request = True
                self._removal_request_after = operation.id
                payload = self._payload_or_none(operation)
                if (
                    not isinstance(
                        payload, (ModelCacheDownloadPayload, ModelCacheRepairPayload)
                    )
                    or payload.cancellation is not None
                ):
                    continue
                manifest = _manifest_of(payload)
                identities = (
                    ArtifactIdentity("model-set", manifest.digest),
                    *(
                        ArtifactIdentity("model-object", digest)
                        for digest in sorted(
                            {item.sha256 for item in manifest.artifacts}
                        )
                    ),
                )
                identity_map = {(item.kind, item.sha256): item for item in identities}
                for gate in session.scalars(
                    select(ArtifactLifecycleGate).where(
                        ArtifactLifecycleGate.removal_owner_kind
                        == "model-cache-operation",
                        ArtifactLifecycleGate.removal_owner_id.is_not(None),
                        or_(
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-set",
                                ArtifactLifecycleGate.artifact_sha256
                                == manifest.digest,
                            ),
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-object",
                                ArtifactLifecycleGate.artifact_sha256.in_(
                                    tuple(item.sha256 for item in manifest.artifacts)
                                ),
                            ),
                        ),
                    )
                ):
                    accepted_requests.append(
                        (
                            operation.id,
                            identity_map[(gate.artifact_kind, gate.artifact_sha256)],
                        )
                    )
            if not observed_request:
                self._removal_request_after = None
        changed = 0
        deadline = time.monotonic() + 0.25
        for request_id, identity in accepted_requests[:limit]:
            if time.monotonic() >= deadline:
                break

            def validate(
                requester: ModelCacheOperation | Job,
                remover: ModelCacheOperation | Job,
                fence: str,
                identity: ArtifactIdentity = identity,
            ) -> bool:
                if not isinstance(requester, ModelCacheOperation) or not isinstance(
                    remover, ModelCacheOperation
                ):
                    return False
                if (
                    requester.kind not in ("download", "repair")
                    or remover.kind != "remove"
                ):
                    return False
                payload = self._payload_or_none(requester)
                removal = _operation_removal(remover)
                if (
                    not isinstance(
                        payload, (ModelCacheDownloadPayload, ModelCacheRepairPayload)
                    )
                    or payload.cancellation is not None
                    or isinstance(removal, Residue)
                ):
                    return False
                manifest = _manifest_of(payload)
                if (
                    requester.artifact_set_sha256 != manifest.digest
                    or payload.artifact_set_sha256 != manifest.digest
                    or requester.plan_digest != payload.plan_digest
                ):
                    return False
                wanted = (
                    manifest.digest == identity.sha256
                    if identity.kind == "model-set"
                    else any(
                        item.sha256 == identity.sha256 for item in manifest.artifacts
                    )
                )
                covered = identity.sha256 in (
                    removal.selected
                    if identity.kind == "model-set"
                    else removal.delete_objects
                )
                return wanted and covered and removal.removal_fence == fence

            def cancel(remover: ModelCacheOperation | Job, accepted_id: str) -> None:
                assert isinstance(remover, ModelCacheOperation)
                reason = f"Removal superseded by accepted model request {accepted_id}"
                self._lifecycle.settle(
                    remover,
                    Reported(Outcome.CANCELLED, effect=Effect.UNKNOWN, reason=reason),
                    self._clock(),
                )
                remover.last_error = reason

            try:
                with (
                    self._model_storage_lock(
                        identity.sha256, model_set=identity.kind == "model-set"
                    ),
                    self._session(write=True) as session,
                ):
                    changed += int(
                        supersede_removal_nowait(
                            session,
                            identity,
                            owner_kind="model-cache-operation",
                            request_id=request_id,
                            validate=validate,
                            cancel=cancel,
                            now=self._clock(),
                        )
                    )
            except (_ArtifactWriterBusy, ArtifactLifecycleError, OSError):
                continue  # the queued request retries; no transfer slot is held
        return changed

    def reconcile_removal_gates(self, *, limit: int = 64) -> int:
        # A retained caller transaction cannot safely precede an artifact lock.
        if isinstance(self._sessions, Session):
            return 0
        with self._session() as session:
            identities = dead_removal_identities(
                session,
                owner_kind="model-cache-operation",
                limit=limit,
                after=self._removal_gate_after,
            )
        if not identities:
            self._removal_gate_after = None
        released = 0
        deadline = time.monotonic() + 0.25
        for identity in identities:
            if time.monotonic() >= deadline:
                break
            self._removal_gate_after = (identity.kind, identity.sha256)
            try:
                with (
                    self._model_storage_lock(
                        identity.sha256, model_set=identity.kind == "model-set"
                    ),
                    self._session(write=True) as session,
                ):
                    changed = release_dead_removal_nowait(
                        session,
                        identity,
                        owner_kind="model-cache-operation",
                        now=self._clock(),
                    )
                if changed:
                    released += 1
                    log_event(
                        _LOGGER,
                        "artifact.removal_gate_reconciled",
                        service="controller",
                        artifact_kind=identity.kind,
                        artifact_sha256=identity.sha256,
                    )
            except (_ArtifactWriterBusy, ArtifactLifecycleError, OSError) as error:
                # The next bounded worker pass retries; storage uncertainty keeps the fence.
                log_event(
                    _LOGGER,
                    "artifact.removal_gate_deferred",
                    service="controller",
                    artifact_kind=identity.kind,
                    artifact_sha256=identity.sha256,
                    code=getattr(error, "code", type(error).__name__),
                )
                continue
        return released

    def advance_removals(self, *, limit: int = 1) -> int:
        """Advance bounded durable model removals without holding transfer slots."""

        if not 1 <= limit <= 100:
            raise InvalidValue("model removal batch limit is invalid")
        self.reconcile_removal_gates()
        now = self._clock()
        with self._session() as session:
            operation_ids = tuple(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(
                        ModelCacheOperation.kind == "remove",
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .where(
                        or_(
                            ModelCacheOperation.next_action_at.is_(None),
                            ModelCacheOperation.next_action_at <= now,
                        ),
                        # A row the startup adoption has not reached still carries
                        # its clock in the payload; it must not take a batch slot
                        # from a due one.  Retired once no such row can exist.
                        or_(
                            ModelCacheOperation.payload["retry"]["next_retry_at"]
                            .as_string()
                            .is_(None),
                            ModelCacheOperation.payload["retry"][
                                "next_retry_at"
                            ].as_string()
                            <= _iso(now),
                        ),
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
                if error.code != ModelCacheCode.PAYLOAD_INVALID:
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
                        self._lifecycle.fail_corrupt(
                            row, f"{error.code}: {error.detail}", now
                        )
                log_event(
                    _LOGGER,
                    ModelCacheCode.REMOVAL_INVALID,
                    service="controller",
                    operation_id=operation_id,
                    code=error.code,
                    detail=error.detail,
                )
        return advanced

    def _retire_removal(
        self, operation_id: str, residue: Residue, *, now: datetime
    ) -> None:
        with self._session(write=True) as session:
            row = session.scalar(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.id == operation_id,
                    ModelCacheOperation.kind == "remove",
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is not None:
                self._retire_unreadable(row, residue, now=now)

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
            if operation.state not in model_cache_states.LIVE:
                return False
            checkpoint = _operation_removal(operation)
            if isinstance(checkpoint, Residue):
                # Damaged and not re-derivable: the row ends (kept for
                # inspection) and unrelated removals carry on.
                self._retire_removal(operation_id, checkpoint, now=now)
                return False
            # One clock: the column (a legacy payload clock is adopted by the
            # adapter, so a row the startup adoption missed is still honoured).
            due = self._lifecycle.lifecycle(operation, now).next_action_at
            if due is not None and due > _aware(now):
                return False
            fence = checkpoint.removal_fence
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            delete_objects = checkpoint.delete_objects
            selected_sets = checkpoint.selected
            in_use = (
                {
                    digest: owners
                    for digest, owners in model_set_reference_reasons(
                        session,
                        session.scalars(
                            select(ModelCacheSet.artifact_set_sha256).where(
                                ModelCacheSet.artifact_set_sha256.in_(
                                    [str(item) for item in selected_sets]
                                )
                            )
                        ).all(),
                    ).items()
                    if owners
                }
                if object_index < len(delete_objects) or set_index < len(selected_sets)
                else {}
            )
        if in_use:
            first_digest = min(in_use)
            self._defer_model_removal(
                operation_id,
                detail=f"waiting for model cache set {first_digest} to be released by: "
                + ", ".join(in_use[first_digest][:4]),
                retry_after_seconds=_RETRY_BASE_SECONDS,
            )
            return False
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
                    if current.object_index != object_index:
                        return False
                    pending_bytes = current.object_pending_bytes
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
                    if current is None or current.set_index != set_index:
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
            payload = self._payload_or_retire(operation, now=now)
            if payload is None or payload.removal_fence != fence:
                return False
            if operation.state not in model_cache_states.LIVE:
                return False
            wait = self._finish_model_removal_in_session(session, operation, now=now)
        if wait is not None:
            # The removal stays fenced and is retried: nothing is half finished.
            self._defer_model_removal(operation_id, detail=wait)
            return False
        return True

    def _finish_model_removal_in_session(
        self, session: Session, operation: ModelCacheOperation, *, now: datetime
    ) -> str | None:
        """Finish a fully reconciled removal; else say what it waits for.

        Returns ``None`` when the removal finished (or its unreadable row was
        retired) and a wait reason when it cannot finish yet: the caller records
        that as an uncertain outcome and the lifecycle core retries it with
        backoff, so a changed scope never ends in a raise.
        """

        checkpoint = _operation_removal(operation)
        if isinstance(checkpoint, Residue):
            self._retire_unreadable(operation, checkpoint, now=now)
            return None
        selected = checkpoint.selected
        delete_objects = checkpoint.delete_objects
        if checkpoint.object_index != len(
            delete_objects
        ) or checkpoint.set_index != len(selected):
            return "model removal cannot finish before every target is reconciled"
        fence = checkpoint.removal_fence
        identities = (
            *(ArtifactIdentity("model-set", str(item)) for item in selected),
            *(ArtifactIdentity("model-object", str(item)) for item in delete_objects),
        )
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
                # A new set references a target: the removal stays fenced and
                # waits (nothing was deleted yet in this transaction).
                return "a new model set references a removal target; removal remains fenced"
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
        result = ModelCacheRemovalResult(
            schema_version=SCHEMA_VERSION,
            removed_entries=list(selected),
            reclaimed_bytes=checkpoint.reclaimed_bytes,
            cancelled_operations=[],
        )
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "completed", now)
        )
        _store_operation_payload(
            operation,
            "remove",
            checkpoint.model_copy(update={"result": result, "failure": None}),
        )
        operation.last_error = None
        self._lifecycle.complete(operation, Reported(Outcome.DONE), now)
        return None

    @staticmethod
    def _newest_readable_revision(
        session: Session, row: CatalogDocumentRevision
    ) -> CatalogDocumentRevision | None:
        """Newest active revision of the same document this Controller can read.

        A revision written under another contract, or since replaced, is not
        usable but must not stop a manifest: the same recipe or model is
        resolved from its newest readable revision instead.
        """

        for candidate in session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == row.kind,
                CatalogDocumentRevision.document_id == row.document_id,
                CatalogDocumentRevision.state == "active",
            )
            .order_by(
                CatalogDocumentRevision.revision_number.desc(),
                CatalogDocumentRevision.created_at.desc(),
                CatalogDocumentRevision.id.desc(),
            )
            .limit(16)
        ):
            try:
                read_catalog_document(candidate)
            except CatalogRevisionContractError:
                continue
            return candidate
        return None

    def _recipe_document(
        self,
        session: Session,
        digest: str | None,
        revision_id: str | None,
        *,
        tolerant: bool = False,
    ) -> tuple[RecipeDefinition, str, str]:
        if revision_id is not None:
            revision = session.get(CatalogDocumentRevision, revision_id)
            if tolerant and revision is not None and revision.kind == "recipe":
                readable = revision.state == "active"
                if readable:
                    try:
                        read_catalog_document(revision)
                    except CatalogRevisionContractError:
                        readable = False
                if not readable:
                    revision = self._newest_readable_revision(session, revision)
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
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_REVISION_MISSING,
                "exact recipe revision is not resolved",
            )
        try:
            recipe = read_catalog_document(revision)
            if not isinstance(recipe, RecipeDefinition):
                raise InvalidType("catalog revision is not a recipe")
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.RECIPE_INVALID, "canonical recipe definition is invalid"
            ) from error
        return recipe, revision.id, revision.content_digest

    def _collect_model_definitions(
        self,
        session: Session,
        digest: str,
        rows: dict[str, CatalogDocumentRevision],
        *,
        visiting: set[str] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(digest, str) or not _is_hex(digest) or len(digest) != 64:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_PIN_INVALID, "model dependency pin is invalid"
            )
        if digest in rows:
            if visiting is not None and digest in visiting:
                raise ModelCacheResolutionInvalid(
                    ModelCacheCode.MODEL_DEPENDENCY_CYCLE,
                    "canonical model dependency graph contains a cycle",
                )
            return
        active = visiting if visiting is not None else set()
        if digest in active:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEPENDENCY_CYCLE,
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
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEFINITION_MISSING,
                "exact model definition is not resolved",
            )
        if aliases is not None:
            try:
                read_catalog_document(row)
            except CatalogRevisionContractError:
                # Tolerant resolution: the same model's newest readable
                # revision stands in, keyed by its own digest.
                substitute = self._newest_readable_revision(session, row)
                if substitute is not None and isinstance(
                    substitute.content_digest, str
                ):
                    aliases[digest] = substitute.content_digest
                    active.remove(digest)
                    self._collect_model_definitions(
                        session,
                        substitute.content_digest,
                        rows,
                        visiting=active,
                        aliases=aliases,
                    )
                    return
        try:
            definition = read_catalog_document(row)
            if not isinstance(definition, ModelDefinition):
                raise InvalidType("catalog revision is not a model")
            rows[digest] = row
            for dependency in definition.dependencies:
                self._collect_model_definitions(
                    session,
                    dependency.content_sha256,
                    rows,
                    visiting=active,
                    aliases=aliases,
                )
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_DEFINITION_INVALID,
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID,
                "catalog artifact identity is incomplete",
            )
        if (
            not isinstance(raw_digest, str)
            or not isinstance(raw_bytes, int)
            or not isinstance(roles, list)
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID,
                "catalog artifact integrity metadata is incomplete",
            )
        if (
            raw_kind not in {"huggingface.file", "github-release.asset"}
            and not self._fixture_sources
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_UNTRUSTED,
                "production cache downloads require a trusted catalog artifact reference",
            )
        source, revision = _source_for_catalog_artifact(value)
        parts = self._parts_from_catalog(value)
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
            parts=parts,
        )
        _validate_artifact(spec)
        return spec

    @staticmethod
    def _parts_from_catalog(
        value: Mapping[str, object],
    ) -> tuple[ArtifactPart, ...] | None:
        """The split parts a catalog file declares, each with its own source URL."""

        raw_parts = value.get("parts")
        if raw_parts is None:
            return None
        if not isinstance(raw_parts, list):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID, "catalog artifact parts are invalid"
            )
        parts: list[ArtifactPart] = []
        for raw in raw_parts:
            if (
                not isinstance(raw, Mapping)
                or not isinstance(raw.get("path"), str)
                or not isinstance(raw.get("sha256"), str)
                or type(raw.get("download_bytes")) is not int
            ):
                raise ModelCacheResolutionRefused(
                    ModelCacheCode.ARTIFACT_INVALID,
                    "catalog artifact part is incomplete",
                )
            # A part is fetched exactly like a file of the same repository and
            # immutable revision, so it gets the same guarded source URL.
            source, _revision = _source_for_catalog_artifact(
                {**value, "path": raw["path"]}
            )
            parts.append(
                ArtifactPart(
                    path=str(raw["path"]),
                    source=source,
                    sha256=str(raw["sha256"]),
                    expected_bytes=int(cast(int, raw["download_bytes"])),
                )
            )
        return tuple(parts)

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
            raw_parts = value.get("parts")
            parts = (
                None
                if raw_parts is None
                else tuple(
                    _part_from_input(raw)
                    for raw in require_sequence(raw_parts, "artifact parts")
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.ARTIFACT_INVALID, "cache artifact input is invalid"
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
            parts=parts,
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
            raise ModelCacheConflictRefused(
                ModelCacheCode.PLAN_INVALID, "download plan digest is invalid"
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
            if replay is not None:
                return replay
        preview = self._download_preview_for_manifest(manifest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.STALE_PLAN, "download preview is stale"
            )
        waiting_for = "; ".join(
            str(item)
            for item in require_sequence(preview["blockers"], "download blockers")
        )
        planned = None if force else preview.get("_transfer")
        transfer = (
            planned
            if isinstance(planned, ModelCacheTransfer)
            else self._transfer_state_for_manifest(manifest, force=force)
        )
        capacity = self._capacity_wait(waiting_for)
        wait_until = None if capacity is None else capacity[1]
        payload = _write_operation_payload(
            "download",
            ModelCacheDownloadPayload(
                schema_version=SCHEMA_VERSION,
                source_policy=SOURCE_POLICY,
                artifact_set_sha256=set_digest,
                manifest=manifest.contract(),
                plan_digest=requested_plan,
                transfer=transfer,
                retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                force_refresh=force,
                selector=selector,
                operator_action="download-model" if selector is not None else None,
                failure=None if capacity is None else capacity[0],
            ),
        )
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
                        allow_pending_removal=True,
                    )
                    self._ensure_set(session, manifest)
                    operation = ModelCacheAdapter.new_operation(
                        request_key=request_key,
                        schema_version=SCHEMA_VERSION,
                        kind="download",
                        next_action_at=wait_until,
                        attempt=1,
                        artifact_set_sha256=set_digest,
                        plan_digest=requested_plan,
                        payload=serialize_json_value(payload),
                        progress=progress_document(
                            self._progress(
                                manifest,
                                phase="queued",
                                expected_bytes=transfer.total_bytes,
                            )
                        ),
                        actor=actor,
                        created_at=now,
                        updated_at=now,
                    )
                    if has_pending_removal(
                        session,
                        (
                            ArtifactIdentity("model-set", set_digest),
                            *(
                                ArtifactIdentity("model-object", item.sha256)
                                for item in manifest.artifacts
                            ),
                        ),
                    ):
                        self._store_failure(
                            operation,
                            _cache_failure(
                                ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                                "Waiting for the prior model removal fence to settle",
                                retryable=True,
                                recovery="retry",
                            ),
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
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another cache operation",
            )
        payload = self._payload_or_none(existing)
        if payload is None:
            # The request key names this operation; an unreadable document
            # cannot contradict it, so the replay returns the stored operation.
            return self._operation_view(existing)
        matches = {
            "selector": payload.selector == selector,
            "refresh": getattr(payload, "force_refresh", False) is force,
        }
        if selector is None:
            matches.update(
                artifact_set=getattr(payload, "artifact_set_sha256", None)
                == artifact_set_sha256,
                plan=existing.plan_digest == plan_digest,
            )
        else:
            matches["operator_action"] = payload.operator_action == "download-model"
        if not all(matches.values()):
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
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
            raise ModelCacheConflictInvalid(
                ModelCacheCode.PIN_MISMATCH,
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
        new_bytes = transfer.total_bytes
        # A split file is assembled part by part, each part deleted once
        # appended: the disk peaks at the file plus its largest part.
        needed = new_bytes + _split_transient_bytes(manifest, cached)
        blockers = []
        if needed > self.free_bytes():
            blockers.append(ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE)
            self._request_storage(
                needed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
            )
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
        if spec.parts is not None:
            return self._split_partial_bytes(set_digest, spec, spec.parts)
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

    def _split_partial_bytes(
        self, set_digest: str, spec: ArtifactSpec, parts: tuple[ArtifactPart, ...]
    ) -> int:
        """Retained bytes of a split file: appended whole parts plus the next part.

        Cheap by design (no hashing): digests are verified where the bytes are
        appended, so this only sizes what a resume will not refetch.
        """

        assembled = self._partial_path(set_digest, spec.sha256)
        try:
            if assembled.is_symlink() or not assembled.is_file():
                size = 0
            else:
                size = assembled.stat().st_size
            if size > spec.expected_bytes:
                size = 0
            appended = 0
            done = 0
            for part in parts:
                if appended + part.expected_bytes > size:
                    break
                appended += part.expected_bytes
                done += 1
            retained = 0
            if done < len(parts):
                next_part = parts[done]
                path = self._partial_path(set_digest, next_part.sha256)
                if not path.is_symlink():
                    if next_part.expected_bytes >= _PARALLEL_RANGE_MIN_BYTES:
                        retained = range_partial_bytes(
                            path,
                            next_part.expected_bytes,
                            workers=_PARALLEL_RANGE_WORKERS,
                        )
                    elif path.is_file():
                        retained = min(path.stat().st_size, next_part.expected_bytes)
            return appended + retained
        except OSError:
            return 0

    def _transfer_state_for_manifest(
        self,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
        cached: frozenset[str] | None = None,
    ) -> ModelCacheTransfer:
        """Create the immutable planned transfer and per-object baselines."""
        artifacts: dict[str, ModelCacheTransferArtifact] = {}
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
                        else self._object_is_stored(spec)
                    )
                    else self._partial_bytes(manifest.digest, spec)
                )
            )
            remaining = max(0, spec.expected_bytes - baseline)
            artifacts[digest] = ModelCacheTransferArtifact(
                baseline_bytes=baseline,
                received_bytes=0,
                started_at=_iso_now(self._clock()),
            )
            total_bytes += remaining
        return ModelCacheTransfer(
            schema_version=SCHEMA_VERSION,
            total_bytes=total_bytes,
            artifacts=artifacts,
        )

    @staticmethod
    def _transfer_totals(payload: ModelCacheDownloadPayload) -> tuple[int, int]:
        transfer = payload.transfer
        return transfer.total_bytes, sum(
            entry.received_bytes for entry in transfer.artifacts.values()
        )

    def _start_transfer(
        self, operation_id: str, *, force: bool
    ) -> tuple[ArtifactSetManifest, str, bool, ModelCacheTransfer] | None:
        """What a claimed download or repair starts from, or ``None`` to skip it.

        The operation is the claim's own record: if it vanished, nothing is left
        to run, and one whose document cannot be read (and cannot be rebuilt from
        its set row) is ended as failed and kept for inspection.  Either way the
        caller moves on to the next operation (rule 5: unknown, never a blocker).
        A missing ``artifact_set_sha256`` column is re-derived from the manifest.
        """

        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                return None
            payload = self._transfer_or_retire(operation)
            if payload is None:
                return None
            manifest = _manifest_of(payload)
            if operation.artifact_set_sha256 is None:
                operation.artifact_set_sha256 = manifest.digest
            return (
                manifest,
                operation.artifact_set_sha256,
                payload.force_refresh or force,
                payload.transfer,
            )

    def _operation_transfer_snapshot(self, operation_id: str) -> tuple[int | None, int]:
        """The ledger's planned total and received bytes; unknown when unreadable."""

        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            payload = None if operation is None else self._transfer_or_none(operation)
            return (None, 0) if payload is None else self._transfer_totals(payload)

    def _transfer_state_for_operation(
        self, operation_id: str
    ) -> ModelCacheTransfer | None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                return None
            payload = self._transfer_or_none(operation)
            return None if payload is None else payload.transfer

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
            stored = self._stored_manifest(row)
            if stored is None:
                # The stored provenance document is damaged and nothing else
                # re-derives it: the manifest in hand (same key) replaces it.
                row.manifest = manifest.document()
            elif stored.digest != manifest.digest:
                raise ModelCacheConflictRefused(
                    ModelCacheCode.IDENTITY_CONFLICT,
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
        transfer: ModelCacheTransfer | None = None,
        previous: ModelCacheOperationProgress | None = None,
    ) -> ModelCacheOperationProgress:
        resolved_bytes = (
            manifest.expected_bytes
            if expected_bytes is _USE_MANIFEST_BYTES
            else expected_bytes
        )
        assert resolved_bytes is None or isinstance(resolved_bytes, int)
        counters = ModelCacheCounters(
            phase=phase,
            completed_artifacts=completed_artifacts,
            total_artifacts=len(manifest.artifacts),
            downloaded_bytes=downloaded_bytes,
            expected_bytes=resolved_bytes,
            current_artifact_key=current_artifact_key,
        )
        members = []
        unique = _unique_artifacts(manifest.artifacts)
        # Progress cannot impose a shard-count limit on installable models.
        # Larger sets retain exact aggregate counters without a partial member list.
        if transfer is not None and len(unique) <= 1024:
            for spec in unique.values():
                entry = transfer.artifacts[spec.sha256]
                baseline, received = entry.baseline_bytes, entry.received_bytes
                members.append(
                    OperationMemberProgress(
                        member_id=spec.sha256,
                        object_sha256=spec.sha256,
                        phase=PHASES[phase],
                        completed_bytes=min(spec.expected_bytes, baseline + received),
                        total_bytes=spec.expected_bytes,
                        state="succeeded"
                        if phase == "completed" or baseline == spec.expected_bytes
                        else "running",
                    )
                )
        return cache_progress(
            counters, previous=previous, now=self._clock(), members=members
        )

    def _run_download(
        self,
        operation_id: str,
        *,
        force: bool,
        interrupt_after_bytes: int | None = None,
    ) -> None:
        started = self._start_transfer(operation_id, force=force)
        if started is None:
            return
        manifest, set_digest, force, transfer = started
        planned_total = transfer.total_bytes
        transfer_attempt = self._mark_running(operation_id)
        if transfer_attempt is None:
            return
        completed = 0
        try:
            with self._lock:
                if not self._publication_allowed(operation_id, set_digest):
                    raise OperationInterrupted(
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
            self._finish_succeeded(
                operation_id,
                ModelCacheDownloadResult(
                    schema_version=SCHEMA_VERSION,
                    artifact_set_sha256=set_digest,
                    coverage="complete",
                ),
            )
        except (OperationInterrupted, InterruptedError) as error:
            self._finish_partial(
                operation_id, set_digest, manifest, str(error) or "download interrupted"
            )
        except (ModelCacheError, OSError, httpx2.HTTPError, ValueError) as error:
            self._finish_failed(
                operation_id,
                set_digest,
                manifest,
                error,
                failed_artifact_key=getattr(error, "failed_artifact_key", None),
                transfer_attempt=transfer_attempt,
            )

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
        except ModelCacheError as error:
            if error.failed_artifact_key is None:
                error.failed_artifact_key = spec.key
            raise
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
                raise OperationInterrupted(
                    "model download cancellation was accepted; partial files preserved"
                )
            is_repair = operation.kind == "repair"
            payload = self._payload_or_none(operation)
            if payload is None:
                # Unreadable bookkeeping interrupts the attempt; the claim loop
                # rebuilds or retires the operation, the transfer ledger on disk
                # (content-addressed) is kept.
                raise OperationInterrupted("cache operation document is unreadable")
            checkpoint = (
                payload.repair_checkpoint
                if force and isinstance(payload, ModelCacheRepairPayload)
                else None
            )
            repaired = checkpoint.completed_objects if checkpoint is not None else []
        if (not force or spec.sha256 in repaired) and self._object_is_stored(spec):
            self._mark_artifact_verified(spec, set_digest)
            return
        if spec.parts is not None:
            self._download_split(
                spec,
                set_digest,
                operation_id=operation_id,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        else:
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
                payload = self._payload_or_none(operation)
                if not isinstance(payload, ModelCacheRepairPayload):
                    raise OperationInterrupted("cache operation document is unreadable")
                checkpoint = payload.repair_checkpoint
                _store_operation_payload(
                    operation,
                    "repair",
                    payload.model_copy(
                        update={
                            "repair_checkpoint": ModelCacheRepairCheckpoint(
                                transfer_id=checkpoint.transfer_id,
                                completed_objects=list(
                                    dict.fromkeys(
                                        [*checkpoint.completed_objects, spec.sha256]
                                    )
                                ),
                            )
                        }
                    ),
                )

    def _download_split(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        """Ingest a file the source hosts only as ordered parts.

        Each part is fetched with the ordinary resumable transfer, then appended
        to one retained temp file while both the part's digest (verified at
        ingress) and the whole file's digest are computed from those same bytes,
        and the part is deleted. The temp file is therefore the only checkpoint:
        its length says which parts are in, a restart cuts it back to a part
        boundary and re-reads that prefix once to recover the digest state, and a
        part that fails its digest is discarded and fetched again. The finished
        file is checked against the declared whole digest, then renamed into the
        object store, so peak extra disk is one part beyond the final file.
        """

        parts = spec.parts
        assert parts is not None
        owner = self._partial_owner(operation_id, set_digest)
        assembled = self._partial_path(owner, spec.sha256)
        assembled.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if assembled.is_symlink() or (assembled.exists() and not assembled.is_file()):
            assembled.unlink()
        boundaries = [0]
        for part in parts:
            boundaries.append(boundaries[-1] + part.expected_bytes)
        size = assembled.stat().st_size if assembled.exists() else 0
        if size > spec.expected_bytes:
            assembled.unlink()
            size = 0
        # Resume from the last whole part already appended: a crash can leave a
        # torn tail, which is cut off (its part is still on disk or refetched).
        done = max(index for index, edge in enumerate(boundaries) if edge <= size)
        if size != boundaries[done]:
            with assembled.open("r+b") as torn:
                torn.truncate(boundaries[done])
                os.fsync(torn.fileno())
        whole = hashlib.sha256()
        if done:
            self._rehash_prefix(assembled, boundaries[done], whole)
        later = {part.sha256 for part in parts[done:]}
        for index in range(done):
            if parts[index].sha256 not in later:
                self._partial_path(owner, parts[index].sha256).unlink(missing_ok=True)
        for index in range(done, len(parts)):
            part_spec = spec.part_spec(index)
            self._download_artifact(
                part_spec,
                set_digest,
                operation_id=operation_id,
                completed_artifacts=0,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
                fetch_only=True,
            )
            self._append_part(
                part_spec,
                self._partial_path(owner, part_spec.sha256),
                assembled,
                whole,
                boundaries[index],
                operation_id=operation_id,
                set_digest=set_digest,
            )
        self._require_transfer_running(operation_id)
        if (
            assembled.stat().st_size != spec.expected_bytes
            or whole.hexdigest() != spec.sha256
        ):
            assembled.unlink(missing_ok=True)
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=0,
                state=ModelFileState.CORRUPT,
            )
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "assembled artifact failed SHA-256 verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        if self._operation_cancellation_pending(operation_id):
            raise OperationInterrupted("model download cancelled during assembly")
        with self._lock:
            if not self._publication_allowed(operation_id, set_digest, spec.sha256):
                raise OperationInterrupted("model download was removed during assembly")
            self._place_object(spec, assembled)
            self._mark_artifact_verified(spec, set_digest)

    @staticmethod
    def _rehash_prefix(path: Path, length: int, digest: hashlib._Hash) -> None:
        remaining = length
        with path.open("rb") as retained:
            while remaining:
                chunk = retained.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.SOURCE_TRUNCATED,
                        "retained assembled bytes are shorter than recorded",
                        recovery="resume",
                    )
                digest.update(chunk)
                remaining -= len(chunk)

    def _append_part(
        self,
        part_spec: ArtifactSpec,
        part: Path,
        assembled: Path,
        whole: hashlib._Hash,
        boundary: int,
        *,
        operation_id: str,
        set_digest: str,
    ) -> None:
        """Append one fetched part, verifying its size and digest in the same pass."""

        if part.is_symlink() or not part.is_file():
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_TRUNCATED,
                "a downloaded part is missing; it is fetched again",
                recovery="resume",
            )
        if part.stat().st_size != part_spec.expected_bytes:
            part.unlink(missing_ok=True)
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_SIZE_MISMATCH,
                "a downloaded part does not have its pinned size; it is fetched again",
                recovery="resume",
            )
        self._checkpoint_artifact(
            part_spec,
            operation_id=operation_id,
            set_digest=set_digest,
            actual_bytes=part_spec.expected_bytes,
            state="verifying",
        )
        stop = self._transfer_stop(operation_id)
        digest = hashlib.sha256()
        with part.open("rb") as source, assembled.open("ab") as output:
            while chunk := source.read(_CHUNK_BYTES):
                if stop.is_set() or self._closed.is_set():
                    raise OperationInterrupted(
                        "model download stopped; partial files preserved"
                    )
                digest.update(chunk)
                whole.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != part_spec.sha256:
            # Cut the bad bytes back off; the next attempt recovers the whole
            # file's digest state from the retained prefix and refetches the part.
            with assembled.open("r+b") as retained:
                retained.truncate(boundary)
                os.fsync(retained.fileno())
            part.unlink(missing_ok=True)
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "downloaded part failed SHA-256 verification; the bytes were discarded and the part restarts",
                recovery="resume",
            )
        # The part's bytes are durably in the assembled file: free the space now.
        part.unlink(missing_ok=True)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        """Entry counts of the service's long-lived collections, for the worker report."""

        return {
            WorkerMemoryComponent.MODEL_CACHE_BACKGROUND_OPERATIONS: len(
                self._background_operations
            ),
            WorkerMemoryComponent.MODEL_CACHE_CANCEL_EVENTS: len(self._cancel_events),
            WorkerMemoryComponent.MODEL_CACHE_PROGRESS_CHECKPOINTS: len(
                self._progress_checkpoint_at
            ),
            WorkerMemoryComponent.MODEL_CACHE_REVERIFIED: len(self._reverified_at),
        }

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
                        state=ModelFileState.PARTIAL,
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
                raise OperationInterrupted(
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

    def _partial_owner(self, operation_id: str, set_digest: str) -> str:
        """The partials directory an operation's retained bytes live under."""

        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None or operation.kind != "repair":
                return set_digest
            payload = self._payload_or_none(operation)
            if not isinstance(payload, ModelCacheRepairPayload):
                return set_digest
            return "repair-" + payload.repair_checkpoint.transfer_id

    def _download_artifact(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        completed_artifacts: int,
        force: bool,
        interrupt_after_bytes: int | None,
        fetch_only: bool = False,
    ) -> None:
        """Fetch one file into its partial and publish it as a cache object.

        ``fetch_only`` (one part of a split file) stops once the retained file
        holds every byte: a part is verified where it is appended to the file it
        belongs to, and is never a cache object.
        """
        partial_owner = self._partial_owner(operation_id, set_digest)
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
        if fetch_only and offset == spec.expected_bytes:
            return
        if offset == spec.expected_bytes and self._verify_file(part, spec):
            with self._lock:
                if not self._publication_allowed(operation_id, set_digest, spec.sha256):
                    raise OperationInterrupted(
                        "model download was removed during verification"
                    )
                self._publish_object(spec, part)
                self._mark_artifact_verified(spec, set_digest)
            return
        if offset == spec.expected_bytes:
            part.unlink(missing_ok=True)
            received = 0
            offset = 0
        if spec.kind == "github-release.asset":
            # Exact local bytes are reusable without provider availability.
            # Missing or partial objects must revalidate the bound release.
            self._validate_github_release_asset(spec)
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
            if fetch_only:
                self._require_transfer_running(operation_id)
                return
            self._complete_download(
                spec, set_digest, part, operation_id, completed_artifacts
            )
            return
        with self._stream_gate(operation_id)():
            self._download_sequential(
                spec,
                set_digest,
                part,
                offset,
                received,
                operation_id=operation_id,
                completed_artifacts=completed_artifacts,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        if fetch_only:
            self._require_transfer_running(operation_id)
            return
        self._complete_download(
            spec, set_digest, part, operation_id, completed_artifacts
        )

    def _require_transfer_running(self, operation_id: str) -> None:
        if self._transfer_stop(operation_id).is_set() or self._closed.is_set():
            raise OperationInterrupted(
                "model download stopped; partial files preserved"
            )

    def _stream_gate(self, operation_id: str):
        stop = self._transfer_stop(operation_id)
        return lambda: self._streams.stream(
            lambda: stop.is_set() or self._closed.is_set()
        )

    def _download_sequential(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        part: Path,
        offset: int,
        received: int,
        *,
        operation_id: str,
        completed_artifacts: int,
        interrupt_after_bytes: int | None,
    ) -> None:
        try:
            stream, effective_offset, close = self._open_source(spec, offset)
        except ModelCacheStorageError as error:
            if error.code == ModelCacheCode.SOURCE_SIZE_MISMATCH:
                part.unlink(missing_ok=True)
            raise
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
                            raise ModelCacheStorageRefused(
                                ModelCacheCode.SOURCE_SIZE_MISMATCH,
                                "source returned more bytes than the immutable artifact pin",
                                recovery="resume",
                            )
                        output.write(chunk)
                        self._streams.record_bytes(len(chunk))
                        received = next_received
                        if (
                            interrupt_after_bytes is not None
                            and received >= interrupt_after_bytes
                        ):
                            raise OperationInterrupted(
                                "download interrupted at a durable checkpoint"
                            )
                        # Ordinary buffered writes remain independent of progress.
                        # Completion/interruption syncs once; a crash resumes from
                        # the actual retained file length, never a progress counter.
                        observe(received)
                except (OSError, httpx2.HTTPError, ModelCacheError):
                    # Preserve even a sub-MiB tail when a source fails. Never
                    # publish its byte count until the sync has succeeded.
                    if received > durable_received:
                        sync_received()
                    raise
                if received > durable_received:
                    sync_received()
        except (OSError, httpx2.HTTPError, ModelCacheError) as error:
            if getattr(error, "code", None) == ModelCacheCode.SOURCE_SIZE_MISMATCH:
                # The source disagrees with the pin; retained bytes are
                # untrusted, so the retry starts again from byte zero.
                part.unlink(missing_ok=True)
                durable_received = 0
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=durable_received,
                state=ModelFileState.PARTIAL,
                completed_artifacts=completed_artifacts,
            )
            if spec.kind == "github-release.asset" and isinstance(
                error, httpx2.HTTPError
            ):
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release asset transfer failed",
                    recovery="resume",
                ) from error
            raise
        finally:
            close()
        if received != spec.expected_bytes:
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state=ModelFileState.PARTIAL,
            )
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_TRUNCATED,
                "source ended before the immutable artifact size",
                recovery="resume",
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
            raise OperationInterrupted(
                "model download stopped; partial files preserved"
            )
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
            # Never keep bytes that failed the pin: discard them so the
            # automatic retry downloads the artifact again from byte zero.
            part.unlink(missing_ok=True)
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=0,
                state=ModelFileState.CORRUPT,
            )
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "downloaded artifact failed SHA-256 verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        if (
            self._transfer_stop(operation_id).is_set()
            or self._closed.is_set()
            or self._operation_cancellation_pending(operation_id)
        ):
            raise OperationInterrupted("model download cancelled during verification")
        # Removal and publication share this process lock.  The durable
        # operation state is checked while holding it, closing the race where
        # a worker verifies an object just as an operator removes its set.
        with self._lock:
            if not self._publication_allowed(operation_id, set_digest, spec.sha256):
                raise OperationInterrupted(
                    "model download was removed during verification"
                )
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
                payload = self._payload_or_none(operation)
                if payload is None or payload.cancellation is not None:
                    return False  # unreadable: not published; it is reconciled
                if payload.removal_fence is not None:
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
        allow_pending_removal: bool = False,
    ) -> dict[str, tuple[str, ...]]:
        try:
            return require_model_sets_open(
                session,
                set_digests,
                now=now,
                object_digests=object_digests,
                allow_pending_removal=allow_pending_removal,
            )
        except ArtifactLifecycleError as error:
            raise ModelCacheConflictRefused(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error

    def _validate_http_download(self, spec: ArtifactSpec) -> None:
        if spec.kind == "github-release.asset":
            try:
                _github_release_asset_binding(spec)
                parsed = urlsplit(spec.source)
            except (ModelCacheResolutionError, TypeError, ValueError) as error:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
                ) from error
            if parsed.scheme != "https" or parsed.hostname != _GITHUB_API_HOST:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_UNTRUSTED,
                    "GitHub release downloads must use the canonical GitHub API host",
                )
            return
        try:
            parsed = urlsplit(spec.source)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            ) from error
        if not self._fixture_sources and (
            parsed.scheme != "https"
            or hostname is None
            or port is not None
            or hostname.lower().rstrip(".") not in self._trusted_source_hosts
            or _is_private_host(hostname)
        ):
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_UNTRUSTED,
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
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
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
                    state=ModelFileState.PARTIAL,
                    completed_artifacts=completed_artifacts,
                )
                return False
            self._range_reserved_bytes += reservation
        client = self._http
        owns_client = client is None
        try:
            if client is None:
                client = httpx2.Client(
                    follow_redirects=False,
                    timeout=httpx2.Timeout(30.0),
                    trust_env=False,
                )

            def open_range(start: int, end: int) -> httpx2.Response:
                headers = {
                    "Range": f"bytes={start}-{end}",
                    "Accept-Encoding": "identity",
                }
                if spec.kind == "github-release.asset":
                    return self._open_github_release_asset(client, spec, headers)
                return self._open_http_response(client, spec.source, headers)

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
                    stream_gate=self._stream_gate(operation_id),
                    on_bytes=self._streams.record_bytes,
                )
            if not completed:
                self._checkpoint_artifact(
                    spec,
                    operation_id=operation_id,
                    set_digest=set_digest,
                    actual_bytes=part.stat().st_size if part.exists() else 0,
                    state=ModelFileState.PARTIAL,
                    completed_artifacts=completed_artifacts,
                )
            return completed
        except (OSError, httpx2.HTTPError, ValueError, ModelCacheError) as error:
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=range_partial_bytes(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                ),
                state=ModelFileState.PARTIAL,
                completed_artifacts=completed_artifacts,
            )
            if spec.kind == "github-release.asset" and isinstance(
                error, httpx2.HTTPError
            ):
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release asset transfer failed",
                    recovery="resume",
                ) from error
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
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            ) from error
        if parsed.scheme == "file":
            if not self._fixture_sources:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_UNTRUSTED,
                    "production cache downloads cannot read file sources",
                )
            path = Path(unquote(parsed.path))
            if path.is_symlink() or not path.is_file():
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "cache file source is unavailable",
                )
            handle = path.open("rb")
            size = path.stat().st_size
            if offset > size:
                handle.close()
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_SIZE_MISMATCH,
                    "cache file source is shorter than its checkpoint",
                )
            handle.seek(offset)
            return handle, offset, handle.close
        self._validate_http_download(spec)
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
                trust_env=False,
            )
        elif not self._fixture_sources and getattr(client, "follow_redirects", False):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "production cache HTTP clients must not follow redirects",
            )
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        response = (
            self._open_github_release_asset(client, spec, headers)
            if spec.kind == "github-release.asset"
            else self._open_http_response(client, spec.source, headers)
        )
        effective_offset = offset
        if offset and response.status_code == 200:
            # The server ignored the range request; restart safely rather than
            # appending a complete payload to a checkpoint.
            response.close()
            response = (
                self._open_github_release_asset(client, spec, {})
                if spec.kind == "github-release.asset"
                else self._open_http_response(client, spec.source, {})
            )
            effective_offset = 0
        if response.status_code == 206:
            content_range = response.headers.get("content-range", "")
            if not content_range.startswith(f"bytes {effective_offset}-"):
                response.close()
                if owns_client:
                    client.close()
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RANGE_INVALID,
                    "cache source returned an invalid byte range",
                )
        return (
            response.iter_bytes(),
            effective_offset,
            lambda: (response.close(), client.close() if owns_client else None),
        )

    @staticmethod
    def _send_anonymous_github_request(
        client: httpx2.Client, url: str, headers: Mapping[str, str]
    ) -> httpx2.Response:
        """Send only these headers, ignoring injected client auth and cookies.

        `Client.build_request` merges client headers and the cookie jar. A raw
        Request plus explicit `auth=None` keeps caller-level credentials away
        from GitHub and its signed asset CDN.
        """

        request_headers = dict(headers)
        request_headers["User-Agent"] = _GITHUB_USER_AGENT
        request = httpx2.Request("GET", url, headers=request_headers)
        return client.send(
            request,
            stream=True,
            auth=None,
            follow_redirects=False,
        )

    def _validate_github_release_asset(self, spec: ArtifactSpec) -> None:
        release_id, asset_id, owner, name = _github_release_asset_binding(spec)
        self._validate_http_download(spec)
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
                trust_env=False,
            )
        release_url = (
            f"https://{_GITHUB_API_HOST}/repos/{owner}/{name}/releases/{release_id}"
        )
        response: httpx2.Response | None = None
        try:
            response = self._send_anonymous_github_request(
                client,
                release_url,
                {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            self._raise_github_http_status(response, allow_binary=False)
            declared_size = response.headers.get("content-length")
            if (
                declared_size is not None
                and declared_size.isdigit()
                and int(declared_size) > _MAX_GITHUB_RELEASE_METADATA_BYTES
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub release metadata exceeds the size limit",
                    recovery="inspect",
                )
            raw = bytearray()
            try:
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_GITHUB_RELEASE_METADATA_BYTES:
                        raise ModelCacheStorageRefused(
                            ModelCacheCode.RELEASE_METADATA_INVALID,
                            "GitHub release metadata exceeds the size limit",
                            recovery="inspect",
                        )
            except httpx2.HTTPError as error:
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release metadata transfer failed",
                    recovery="resume",
                ) from error
            try:
                release = _GitHubReleaseMetadata.model_validate_json(raw)
            except (TypeError, ValueError, ValidationError) as error:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub release metadata does not match the required response fields",
                    recovery="inspect",
                ) from error
            if release.id != release_id:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub returned metadata for a different or invalid release",
                    recovery="inspect",
                )
            matches = [asset for asset in release.assets if asset.id == asset_id]
            if len(matches) != 1:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned asset ID is not a unique member of the pinned GitHub release",
                    recovery="inspect",
                )
            asset = matches[0]
            if (
                asset.name != Path(spec.path).name
                or asset.size != spec.expected_bytes
                or asset.state != "uploaded"
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned GitHub release asset name, state, or size does not match the model file",
                    recovery="inspect",
                )
            provider_digest = asset.digest
            if provider_digest is not None and (
                not isinstance(provider_digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", provider_digest) is None
                or provider_digest.removeprefix("sha256:") != spec.sha256
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned GitHub release asset digest does not match the model file",
                    recovery="inspect",
                )
        finally:
            if response is not None:
                response.close()
            if owns_client:
                client.close()

    def _raise_github_http_status(
        self, response: httpx2.Response, *, allow_binary: bool
    ) -> None:
        status = response.status_code
        remaining = response.headers.get("x-ratelimit-remaining")
        retry_after_header = response.headers.get("retry-after")
        secondary_rate_limit = False
        if status in {403, 429} and remaining != "0" and not retry_after_header:
            secondary_rate_limit = self._github_error_reports_secondary_rate_limit(
                response
            )
        rate_limited = status == 429 or (
            status == 403
            and (remaining == "0" or bool(retry_after_header) or secondary_rate_limit)
        )
        if rate_limited:
            retry_after = _retry_after_seconds(response.headers, now=self._clock())
            if secondary_rate_limit and retry_after is None and remaining != "0":
                # GitHub's secondary-limit guidance asks clients to wait at
                # least one minute when it supplies no explicit retry hint.
                retry_after = 60
            response.close()
            raise ModelCacheStorageRefused(
                ModelCacheCode.RATE_LIMITED,
                "GitHub rate limited this anonymous release download; it will resume automatically",
                retry_after_seconds=retry_after,
                recovery="resume",
            )
        if response.status_code in ({200, 206} if allow_binary else {200}):
            return
        if response.status_code in {301, 302, 303, 307, 308} and allow_binary:
            return
        response.close()
        if 300 <= status < 400:
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub release request used an unsupported redirect status",
                recovery="inspect",
            )
        if status in {401, 403}:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_SOURCE_ACCESS_DENIED.value,
                "GitHub denied anonymous access to the public release source",
                recovery="inspect",
            )
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE,
            f"GitHub release request failed with status {status}",
            recovery="resume",
            source_status=status,
        )

    @staticmethod
    def _github_error_reports_secondary_rate_limit(
        response: httpx2.Response,
    ) -> bool:
        """Read only a small typed error body; never persist its message."""

        raw = bytearray()
        try:
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > _MAX_GITHUB_ERROR_METADATA_BYTES:
                    return False
                raw.extend(chunk)
            error = _GitHubErrorMetadata.model_validate_json(raw)
        except (httpx2.HTTPError, TypeError, ValueError, ValidationError):
            return False
        return "secondary rate limit" in error.message.casefold()

    def _open_github_release_asset(
        self,
        client: httpx2.Client,
        spec: ArtifactSpec,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        _github_release_asset_binding(spec)
        self._validate_http_download(spec)
        request_headers = {
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
            **headers,
        }
        try:
            response = self._send_anonymous_github_request(
                client, spec.source, request_headers
            )
        except httpx2.HTTPError as error:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub release asset request failed",
                recovery="resume",
            ) from error
        self._raise_github_http_status(response, allow_binary=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub asset redirect did not provide a destination",
                recovery="inspect",
            )
        redirected_url = urljoin(spec.source, location)
        if not _is_allowed_github_release_redirect(redirected_url):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub asset redirected outside the trusted release CDN",
                recovery="inspect",
            )
        try:
            response = self._send_anonymous_github_request(
                client,
                redirected_url,
                {
                    key: value
                    for key, value in request_headers.items()
                    if key in {"Range", "Accept-Encoding"}
                },
            )
        except httpx2.HTTPError as error:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub release asset transfer failed",
                recovery="resume",
            ) from error
        self._raise_github_http_status(response, allow_binary=True)
        if 300 <= response.status_code < 400:
            response.close()
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub release CDN returned a second redirect",
                recovery="inspect",
            )
        return response

    def _open_http_response(
        self,
        client: httpx2.Client,
        source: str,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
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
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RATE_LIMITED,
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
                if source_is_huggingface:
                    self._streams.throttled(
                        retry_after, "Hugging Face answered 429 (rate limited)"
                    )
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RATE_LIMITED,
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
                    raise ModelCacheStorageRefused(
                        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value,
                        "Hugging Face could not authorize this download; verify account access and token scope at "
                        f"{_huggingface_access_url(source)}; the download resumes automatically when the token changes",
                        recovery="access_denied",
                    )
                raise ModelCacheStorageRefused(
                    ModelCacheCode.CREDENTIALS_MISSING,
                    "Hugging Face access is required; request access at "
                    f"{_huggingface_access_url(source)} and configure HF_TOKEN_FILE; the download resumes automatically when the token changes",
                    recovery="access_required",
                )
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REDIRECT_FORBIDDEN,
                        "cache source redirect did not provide a destination",
                    )
                redirected_url = urljoin(current_url, location)
                if not source_is_huggingface or not _is_allowed_huggingface_redirect(
                    redirected_url
                ):
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REDIRECT_FORBIDDEN,
                        "cache source redirected outside the trusted Hugging Face authorities",
                    )
                current_url = redirected_url
                continue
            if status_code not in {200, 206}:
                retry_after = (
                    _retry_after_seconds(response.headers, now=self._clock())
                    if status_code >= 500
                    else None
                )
                response.close()
                if status_code >= 500 and source_is_huggingface:
                    self._streams.throttled(
                        retry_after, f"Hugging Face answered {status_code}"
                    )
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    f"cache source request failed with status {status_code}",
                    retry_after_seconds=retry_after,
                    source_status=status_code,
                )
            return response
        raise ModelCacheStorageRefused(
            ModelCacheCode.REDIRECT_FORBIDDEN,
            "cache source exceeded the trusted Hugging Face redirect limit",
        )

    def _huggingface_credential_fingerprint(self) -> str:
        """Identify the configured credential file without reading the secret."""

        path = self._huggingface_token_path
        if path is None:
            return "unconfigured"
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unreadable"
        return f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"

    def _resume_after_credential_change(self) -> int:
        """Requeue authorization failures once the credential file changed."""

        current = self._huggingface_credential_fingerprint()
        if current == self._observed_credential_fingerprint:
            return 0
        now = self._clock()
        resumed = 0
        with self._lock, self._session(write=True) as session:
            rows = list(
                session.scalars(
                    select(ModelCacheOperation)
                    .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                    .where(ModelCacheOperation.state == "failed")
                    .order_by(ModelCacheOperation.updated_at.desc())
                    .limit(256)
                    .with_for_update(skip_locked=True)
                )
            )
            for operation in rows:
                failure = self._canonical_failure(operation)
                if (
                    failure is None
                    or failure.code not in _CREDENTIAL_FAILURE_PUBLIC_CODES
                ):
                    continue
                payload = self._payload_or_none(operation)
                if payload is None or payload.cancellation is not None:
                    continue
                retry = payload.retry
                if retry.credential_fingerprint == current:
                    continue
                newer = session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.artifact_set_sha256
                        == operation.artifact_set_sha256,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
                        ModelCacheOperation.created_at > operation.created_at,
                    )
                )
                if newer:
                    # A later request owns this artifact set now.
                    continue
                _store_operation_payload(
                    operation,
                    operation.kind,
                    payload.model_copy(
                        update={
                            "retry": retry.model_copy(
                                update={"credential_fingerprint": None}
                            )
                        }
                    ),
                )
                # The credential changed: withdraw the end and let the core decide
                # again (a queued, immediately claimable operation).
                self._lifecycle.reopen(operation, now)
                self._lifecycle.settle(operation, Tick(), now)
                operation.attempt = int(operation.attempt) + 1
                operation.progress = progress_document(
                    cache_phase(_operation_progress(operation), "queued", now)
                )
                resumed += 1
        self._observed_credential_fingerprint = current
        return resumed

    def _load_huggingface_token(self) -> str | None:
        path = self._huggingface_token_path
        if path is None:
            return None
        try:
            if path.is_symlink():
                raise ModelCacheCredentialPathUnsafe(
                    "Hugging Face credential path is unsafe"
                )
            if not path.exists():
                return None
            if not path.is_file():
                raise ModelCacheCredentialPathUnsafe(
                    "Hugging Face credential path is unsafe"
                )
            if path.stat().st_size == 0:
                return None
            raw = read_runtime_secret(path)
        except (OSError, RuntimeSecretError):
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file is unavailable; configure HF_TOKEN_FILE",
                recovery="credentials_invalid",
            ) from None
        value = raw.strip()
        if not value:
            return None
        try:
            token = value.decode("ascii")
        except UnicodeDecodeError:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file must contain one ASCII bearer token",
                recovery="credentials_invalid",
            ) from None
        if any(character.isspace() for character in token) or "\x00" in token:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file must contain one bearer token",
                recovery="credentials_invalid",
            )
        return token

    def _verify_file(self, path: Path, spec: ArtifactSpec) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        return verified_files.verify_path(path, spec.sha256, spec.expected_bytes)

    def _object_is_stored(self, spec: ArtifactSpec) -> bool:
        """Whether the object is in the cache: receipt plus exact size.

        Bytes are hashed once, where they enter from Hugging Face, before the
        object is published and its receipt written. Reuse trusts that.
        """
        return self._object_is_available(spec.sha256, spec.expected_bytes)

    def _manifest_coverage_complete(self, manifest: ArtifactSetManifest) -> bool:
        """Whether every unique object is stored (receipt and size), including empty ones."""
        return all(
            self._object_is_stored(spec)
            for spec in _unique_artifacts(manifest.artifacts).values()
        )

    def _publish_object(self, spec: ArtifactSpec, part: Path) -> None:
        if not self._verify_file(part, spec):
            part.unlink(missing_ok=True)
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "cache artifact failed verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        self._place_object(spec, part)

    def _place_object(self, spec: ArtifactSpec, verified: Path) -> None:
        """Atomically install bytes whose digest was already verified at ingress."""

        target = self._object_path(spec.sha256)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        # Atomic overwrite preserves both the pathname and already-open
        # readers until the verified replacement is ready. Moving the old
        # object aside first creates an availability gap (and a crash window).
        os.replace(verified, target)
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
                    prior = _operation_progress(operation).measurement
                    if (
                        adopt_progress_phase(prior.phase) is ProgressPhase.DOWNLOADING
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
                payload = (
                    None if operation is None else self._transfer_or_none(operation)
                )
                if operation is not None and payload is not None:
                    # (an unreadable document is not checkpointed: the bytes
                    # are content-addressed and the claim loop reconciles it)
                    manifest = _manifest_of(payload)
                    # A part of a split file reports on the whole file's ledger
                    # entry, offset by the parts already appended.
                    ledger_digest = spec.ledger_sha256 or spec.sha256
                    actual_bytes += spec.ledger_base
                    entry = payload.transfer.artifacts.get(ledger_digest)
                    baseline = 0 if entry is None else entry.baseline_bytes
                    previous_received = 0 if entry is None else entry.received_bytes
                    artifacts = {
                        **payload.transfer.artifacts,
                        ledger_digest: ModelCacheTransferArtifact(
                            baseline_bytes=baseline,
                            received_bytes=max(
                                previous_received, max(0, actual_bytes - baseline)
                            ),
                            started_at=entry.started_at
                            if entry is not None
                            else _iso_now(self._clock()),
                        ),
                    }
                    transfer = payload.transfer.model_copy(
                        update={"artifacts": artifacts}
                    )
                    total = transfer.total_bytes
                    received = sum(value.received_bytes for value in artifacts.values())
                    payload = payload.model_copy(update={"transfer": transfer})
                    _store_operation_payload(operation, operation.kind, payload)
                    old_progress = _operation_progress(operation)
                    old_downloaded = old_progress.downloaded_bytes
                    old_completed = old_progress.completed_artifacts
                    # A checkpoint is a heartbeat: it renews this process's lease;
                    # under another fence, or after the core put the operation
                    # back to wait, it changes nothing (rule 7).
                    self._lifecycle.renew(
                        operation,
                        self._claim_owner,
                        _TRANSFER_CLAIM_SECONDS,
                        now,
                        take=False,
                    )
                    operation.progress = progress_document(
                        self._progress(
                            manifest,
                            previous=old_progress,
                            phase="downloading"
                            if state == ModelFileState.PARTIAL
                            else "verifying",
                            completed_artifacts=max(old_completed, completed_artifacts),
                            downloaded_bytes=max(old_downloaded, received),
                            expected_bytes=total,
                            current_artifact_key=spec.key,
                            transfer=transfer,
                        )
                    )
                    operation.current_artifact_key = spec.key
                    operation.updated_at = now
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = (
                        "downloading"
                        if state == ModelFileState.PARTIAL
                        else "verifying"
                    )
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
        manifest = self._stored_manifest(row)
        if manifest is None:
            return 0  # unknown: counted again once the manifest is re-derived
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
                    operation.last_error = detail[:512]
                    # Interrupted work is uncertain, not failed: the core retries
                    # the exact transfer (nothing here is irreversible).
                    self._lifecycle.complete(
                        operation,
                        Reported(
                            Outcome.UNKNOWN,
                            fence=self._claim_owner,
                            reason=detail[:512],
                        ),
                        now,
                        interrupted=True,
                        consume_retry=True,
                    )
                    current = self._transfer_or_none(operation)
                    if current is not None:
                        payload = current.model_copy(
                            update={
                                "failure": _cache_failure(
                                    ModelCacheCode.INTERRUPTED,
                                    f"{detail[:480]}; preserved bytes remain available to resume",
                                    retryable=True,
                                    recovery="resume",
                                )
                            }
                        )
                        _store_operation_payload(operation, operation.kind, payload)
                        _total, received = self._transfer_totals(payload)
                        previous_progress = _operation_progress(operation)
                        operation.progress = progress_document(
                            self._progress(
                                manifest,
                                previous=previous_progress,
                                phase="downloading",
                                completed_artifacts=previous_progress.completed_artifacts,
                                downloaded_bytes=received,
                                expected_bytes=_total,
                                current_artifact_key=operation.current_artifact_key,
                                transfer=payload.transfer,
                            )
                        )
                        operation.progress = progress_document(
                            cache_phase(
                                _operation_progress(operation),
                                "downloading",
                                now,
                                waiting=True,
                            )
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
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update={"nowait": True}
            )
            if operation is None or operation.state != "running":
                return
            payload = self._payload_or_none(operation)
            if payload is None:
                return  # unreadable: the lease lapses and the claim loop reconciles
            if payload.cancellation is not None:
                self._transfer_stop(operation_id).set()
                return
            row = self._lifecycle.lifecycle(operation, now)
            if (
                row.state is not State.RUNNING
                or row.fence not in (None, self._claim_owner)
                or (
                    row.lease_deadline is not None and row.lease_deadline <= _aware(now)
                )
            ):
                return  # the lease is another process's (or has lapsed): not ours
            # A dependency wait, not a consumed attempt: the owner of the busy
            # lock releases it and the same attempt resumes.
            self._lifecycle.complete(
                operation,
                Reported(
                    Outcome.UNKNOWN,
                    fence=self._claim_owner,
                    retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                    reason=error.detail,
                ),
                now,
                interrupted=False,
                consume_retry=False,
            )
            next_retry = operation.next_action_at
            if next_retry is None:
                return
            delay = max(1, round((_aware(next_retry) - now).total_seconds()))
            self._store_failure(
                operation,
                _cache_failure(
                    error.code,
                    error.detail,
                    retryable=True,
                    recovery="resume",
                    retry_time=_iso(next_retry),
                    retry_after_seconds=delay,
                    artifact_key=artifact_key,
                ),
            )
            operation.last_error = error.detail
            operation.progress = progress_document(
                cache_phase(_operation_progress(operation), "queued", now, waiting=True)
            )

    def _finish_failed(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        error: BaseException,
        failed_artifact_key: str | None = None,
        transfer_attempt: int | None = None,
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
                measured_free = shutil.disk_usage(self._root).free
                self._request_storage(measured_required, ModelCacheCode.CAPACITY)
                required_bytes, free_bytes, shortfall_bytes = (
                    measured_required,
                    measured_free,
                    max(0, measured_required - measured_free),
                )
            except (OSError, RuntimeError, ValueError):
                pass
        failure_code = getattr(error, "code", None)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            failure_code = ModelCacheCode.CAPACITY
        if (
            not isinstance(failure_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", failure_code) is None
        ):
            failure_code = ModelCacheCode.OPERATION_FAILED
        cancellation_pending = False
        source_status = getattr(error, "source_status", None)
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is not None and operation.state == "cancelled":
                return
            if transfer_attempt is not None and source_status in _SOURCE_GONE_STATUSES:
                if operation is None:
                    return
                claim = self._lifecycle.lifecycle(operation, now)
                if (
                    claim.state is not State.RUNNING
                    or claim.fence != self._claim_owner
                    or claim.attempt != transfer_attempt
                    or claim.lease_deadline is None
                    or claim.lease_deadline <= _aware(now)
                ):
                    # The same failed Future may be reported again after a
                    # lost commit acknowledgement. Only its exact live attempt
                    # can contribute a new provider file observation.
                    return
            cancellation_pending = (
                operation is not None and _operation_cancellation(operation) is not None
            )
            row = (
                None if cancellation_pending else session.get(ModelCacheSet, set_digest)
            )
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                all_valid = all(
                    self._object_is_stored(spec) for spec in manifest.artifacts
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
                payload_before = self._payload_or_none(operation)
                retained_retry = (
                    payload_before.retry if payload_before is not None else None
                )
                gone_recovery: str | None = None
                missing_source: ModelCacheMissingSourceObservation | None = None
                artifact_key = failed_artifact_key or operation.current_artifact_key
                if (
                    retryable
                    and transfer_attempt is not None
                    and source_status in _SOURCE_GONE_STATUSES
                    and artifact_key
                ):
                    previous_observation = (
                        retained_retry.missing_source
                        if retained_retry is not None
                        else None
                    )
                    observations = (
                        previous_observation.observations + 1
                        if previous_observation is not None
                        and previous_observation.artifact_key == artifact_key
                        and previous_observation.status == source_status
                        else 1
                    )
                    missing_source = ModelCacheMissingSourceObservation(
                        artifact_key=artifact_key,
                        status=404 if source_status == 404 else 410,
                        observations=observations,
                    )
                    if observations >= _SOURCE_GONE_ATTEMPTS:
                        # Observed gone, not a blocker: end the download with a
                        # typed reason naming the file. A new download request
                        # resolves the model's newest catalog revision.
                        retryable = False
                        failure_code = ModelCacheCode.SOURCE_GONE
                        gone_recovery = "download_again"
                        detail = self._source_gone_detail(
                            session,
                            manifest,
                            failed_artifact_key or operation.current_artifact_key,
                            int(source_status),
                            observations,
                        )
                        if row is not None:
                            row.last_error = detail

                operator_retries = (
                    0
                    if payload_before is None
                    else payload_before.retry.operator_retries
                )
                if operation.state == "failed":
                    # A recheck that finds the failure transient: withdraw the
                    # end, and let the core decide again (retry, with backoff).
                    self._lifecycle.reopen(operation, now)
                provider_hint = getattr(error, "retry_after_seconds", None)
                operation.last_error = detail[:512]
                # The core owns the schedule (bounded backoff, never earlier than
                # the provider's own hint) and the end of a terminal failure.
                settled = self._lifecycle.complete(
                    operation,
                    Reported(
                        Outcome.FAILED,
                        fence=self._claim_owner,
                        retryable=retryable,
                        retry_after=(
                            now + timedelta(seconds=provider_hint)
                            if retryable
                            and type(provider_hint) is int
                            and provider_hint >= 0
                            else None
                        ),
                        reason=detail[:512],
                    ),
                    now,
                    interrupted=False,
                )
                if settled.state not in (State.BACKOFF, State.FAILED):
                    return  # a report under a fence that is not this worker's
                bounded_retry = settled.state is State.BACKOFF
                next_retry = operation.next_action_at if bounded_retry else None
                retry_delay = (
                    max(1, round((_aware(next_retry) - now).total_seconds()))
                    if next_retry is not None
                    else None
                )
                payload_after = self._payload_or_none(operation)
                retry = (
                    None
                    if payload_after is None
                    else payload_after.retry.model_copy(
                        update={
                            "operator_retries": operator_retries,
                            "missing_source": missing_source,
                        }
                    )
                )
                if retry is not None and failure_code in _CREDENTIAL_FAILURE_CODES:
                    # The worker resumes this exact transfer once the
                    # configured credential file changes.
                    retry = retry.model_copy(
                        update={
                            "credential_fingerprint": (
                                self._huggingface_credential_fingerprint()
                            )
                        }
                    )
                provider_rate_limited = (
                    getattr(error, "code", None) == ModelCacheCode.RATE_LIMITED
                )
                if provider_rate_limited and self._manifest_has_huggingface_source(
                    manifest
                ):
                    self._record_huggingface_cooldown(
                        next_retry or now + timedelta(seconds=_RETRY_BASE_SECONDS)
                    )
                failure_payload = _cache_failure(
                    failure_code,
                    detail,
                    retryable=retryable,
                    recovery=gone_recovery
                    or getattr(error, "recovery", None)
                    or ("capacity" if failure_code == ModelCacheCode.CAPACITY else None)
                    or ("resume" if bounded_retry else "retry"),
                    retry_time=_iso(next_retry) if bounded_retry else None,
                    retry_after_seconds=retry_delay if bounded_retry else None,
                    required_bytes=required_bytes,
                    free_bytes=free_bytes,
                    shortfall_bytes=shortfall_bytes,
                    artifact_key=failed_artifact_key,
                )
                if payload_after is not None and retry is not None:
                    _store_operation_payload(
                        operation,
                        operation.kind,
                        payload_after.model_copy(
                            update={"failure": failure_payload, "retry": retry}
                        ),
                    )
                operation.progress = progress_document(
                    cache_phase(
                        _operation_progress(operation),
                        "queued" if bounded_retry else "failed",
                        now,
                    )
                )
        if cancellation_pending:
            self._try_settle_cancellation(operation_id)

    @staticmethod
    def _source_gone_detail(
        session: Session,
        manifest: ArtifactSetManifest,
        artifact_key: str | None,
        status: int,
        attempts: int,
    ) -> str:
        """Name the missing file, and say whether the catalog already has a successor."""

        spec = next(
            (
                item
                for item in manifest.artifacts
                if artifact_key in {item.key, item.artifact_id}
            ),
            None,
        )
        where = "a file"
        if spec is not None:
            repository = (spec.repository or "").removeprefix("https://huggingface.co/")
            revision = (spec.revision or "")[:12]
            where = f"file {spec.path}" + (
                f" ({repository}@{revision})" if repository or revision else ""
            )
        successor = ModelCacheService._model_update_candidate(session, manifest)
        action = (
            "a newer catalog revision of this model exists; download it again to use it"
            if successor is not None
            else "the model needs a catalog refresh before it can be downloaded again"
        )
        return (
            f"source gone: {where} answered HTTP {status} on {attempts} attempts; "
            f"{action}"
        )[:512]

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
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
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
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                return self._operation_view(existing)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != "failed"
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.OPERATION_NOT_RETRYABLE,
                    "cache operation is not retryable",
                )
            previous_payload = self._transfer_or_retire(previous)
            if previous_payload is None:
                # Nothing readable to retry from: the unreadable operation stays
                # ended (kept for inspection) and the caller sees it as it is; a
                # new download request starts the work again.
                return self._operation_view(previous)
            now = self._clock()
            # The set is the manifest's own digest; the column is bookkeeping.
            previous_set = (
                previous.artifact_set_sha256 or _manifest_of(previous_payload).digest
            )
            self._require_model_sets_open(session, (previous_set,), now=now)
            payload = previous_payload.model_copy(
                update={
                    "retry": previous_payload.retry.model_copy(
                        update={
                            "automatic_attempts": 1,
                            "operator_retries": previous_payload.retry.operator_retries
                            + 1,
                        }
                    ),
                    "retry_of": previous.id,
                }
            )
            previous_progress = _operation_progress(previous)
            operation = ModelCacheAdapter.new_operation(
                request_key=request_key,
                schema_version=2,
                kind=previous.kind,
                attempt=1,
                artifact_set_sha256=previous_set,
                plan_digest=previous.plan_digest,
                payload=serialize_json_value(
                    _write_operation_payload(previous.kind, payload)
                ),
                progress=progress_document(
                    cache_phase(previous_progress, "queued", now)
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
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            if (
                previous.artifact_set_sha256 != requested_set
                or previous.plan_digest != requested_plan
            ):
                raise ModelCacheConflictRefused(
                    ModelCacheCode.IDENTITY_MISMATCH,
                    "access recheck identity does not match the persisted operation",
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.id == previous.id:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "access recheck requires a new operator request key",
                    )
                if (
                    existing.kind == previous.kind
                    and existing.artifact_set_sha256 == previous.artifact_set_sha256
                    and existing.plan_digest == previous.plan_digest
                ):
                    return self._operation_view(existing)
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.REQUEST_KEY_REUSED,
                    "request key was already used for another cache operation",
                )
            failure = self._canonical_failure(previous)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != "failed"
                or failure is None
                or failure.code not in auth_codes
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                    "the operation does not have a terminal Hugging Face access failure",
                )
            previous_payload = self._transfer_or_retire(previous)
            if previous_payload is None:
                return self._operation_view(previous)  # unreadable: retired
            prior_check = previous_payload.access_recheck
            if prior_check is not None and prior_check.request_key == request_key:
                return self._operation_view(previous)
            manifest = _manifest_of(previous_payload)
            failed_artifact_key = failure.artifact_key or previous.current_artifact_key

        try:
            self._check_huggingface_access(
                manifest,
                failed_artifact_key=(
                    failed_artifact_key
                    if isinstance(failed_artifact_key, str)
                    else None
                ),
            )
        except (ModelCacheStorageError, httpx2.HTTPError, OSError) as error:
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
                    raise ModelCacheNotFoundInvalid(
                        ModelCacheCode.OPERATION_MISSING,
                        "cache operation was not found",
                    )
                previous.last_error = safe_detail
                self._lifecycle.complete(
                    previous,
                    Reported(Outcome.FAILED, retryable=False, reason=safe_detail),
                    now,
                )
                current = self._payload_or_none(previous)
                if current is not None:
                    _store_operation_payload(
                        previous,
                        previous.kind,
                        current.model_copy(
                            update={
                                "failure": failure_payload,
                                "access_recheck": ModelCacheAccessRecheck(
                                    request_key=request_key,
                                    checked_at=_iso_now(now),
                                    authorized=False,
                                ),
                            }
                        ),
                    )
                return self._operation_view(previous)

        now = self._clock()
        with self._lock, self._session(write=True) as session:
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if previous is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            current_payload = self._transfer_or_retire(previous, now=now)
            if current_payload is None:
                return self._operation_view(previous)  # unreadable: retired
            payload = current_payload.model_copy(
                update={
                    "failure": None,
                    "result": None,
                    "claim": None,
                    "retry": current_payload.retry.model_copy(
                        update={
                            "automatic_attempts": 1,
                            "next_retry_at": None,
                            "retry_after_seconds": None,
                        }
                    ),
                    "resume_of": previous.id,
                    "access_recheck": ModelCacheAccessRecheck(
                        request_key=request_key,
                        checked_at=_iso_now(now),
                        authorized=True,
                    ),
                }
            )
            total, received = self._transfer_totals(payload)
            prior_progress = _operation_progress(previous)
            self._require_model_sets_open(session, (requested_set,), now=now)
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
                transfer=payload.transfer,
            )
            operation = ModelCacheAdapter.new_operation(
                request_key=request_key,
                schema_version=SCHEMA_VERSION,
                kind=previous.kind,
                attempt=1,
                artifact_set_sha256=requested_set,
                plan_digest=previous.plan_digest,
                payload=serialize_json_value(
                    _write_operation_payload(previous.kind, payload)
                ),
                progress=progress_document(progress),
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
            raise ModelCacheConflictInvalid(
                ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                "the persisted operation has no canonical Hugging Face source to check",
            )
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
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

    def _mark_running(self, operation_id: str) -> int | None:
        """The worker starts (or resumes) the transfer: claim or renew, then run."""

        stop = self._transfer_stop(operation_id)
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = self._payload_or_none(operation)
            if payload is None or payload.cancellation is not None:
                return  # unreadable: the claim loop reconciles it
            if self._lifecycle.renew(
                operation, self._claim_owner, _TRANSFER_CLAIM_SECONDS, now
            ):
                _store_operation_payload(
                    operation,
                    operation.kind,
                    payload.model_copy(update={"failure": None}),
                )
                # A settled failed transfer stopped its siblings. A newly
                # accepted attempt resumes; durable cancellation above wins.
                stop.clear()
                return operation.attempt
        return None

    def _finish_succeeded(
        self, operation_id: str, result: ModelCacheDownloadResult
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = self._transfer_or_none(operation)
            if payload is not None and payload.cancellation is not None:
                return
            if payload is not None:
                # The bytes are in storage whatever the document says: an
                # unreadable one only loses the result note, never the success.
                _store_operation_payload(
                    operation,
                    operation.kind,
                    _updated(payload, result=result, failure=None),
                )
            operation.progress = progress_document(
                cache_phase(_operation_progress(operation), "completed", now)
            )
            self._lifecycle.complete(
                operation, Reported(Outcome.DONE, fence=self._claim_owner), now
            )

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
        transfer: ModelCacheTransfer | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = self._transfer_or_none(operation)
            if payload is None:
                return  # unreadable: the claim loop reconciles it
            if payload.cancellation is not None:
                self._transfer_stop(operation_id).set()
                return
            old_progress = _operation_progress(operation)
            old_downloaded = old_progress.downloaded_bytes
            old_completed = old_progress.completed_artifacts
            operation.progress = progress_document(
                self._progress(
                    manifest,
                    previous=old_progress,
                    phase=phase,
                    completed_artifacts=max(old_completed, completed_artifacts),
                    downloaded_bytes=max(old_downloaded, downloaded_bytes),
                    expected_bytes=expected_bytes,
                    current_artifact_key=current_artifact_key,
                    transfer=transfer if transfer is not None else payload.transfer,
                )
            )
            operation.current_artifact_key = current_artifact_key
            self._lifecycle.renew(
                operation, self._claim_owner, _TRANSFER_CLAIM_SECONDS, now, take=False
            )

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

    def _try_settle_cancellation(
        self, operation_id: str, *, forced: bool = True
    ) -> bool:
        """Drive a durable cancel intent through the core; ``True`` once it ended.

        The core stops the transfer (idempotently), observes until no writer of
        the operation's objects is active, and ends the cancel as ``cancelled``
        (rule 4).  A stop that stays unconfirmed past its budget ends it anyway
        with the effect unknown and a residue note: a cancel never waits for an
        operator.  ``forced`` (the cancel itself and a worker that just ended)
        decides now; the reconciler's pass decides when the core's clock says.
        """

        now = self._clock()
        with self._session(write=True) as session:
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return True  # gone: there is nothing left to cancel
            if operation.state == "cancelled":
                return True
            cancellation = _operation_cancellation(operation)
            if cancellation is None:
                return False
            if operation.kind != "download" or operation.artifact_set_sha256 is None:
                return False
            row = self._lifecycle.lifecycle(operation, now)
            when = (
                max(_aware(now), row.next_action_at)
                if forced and row.next_action_at is not None
                else now
            )
            event = (
                CancelRequested(cancellation.request_key, cancellation.reason)
                if row.state is not State.OBSERVING
                else Tick()
            )
            settled = self._lifecycle.settle(operation, event, when)
            return settled.state is State.CANCELLED

    def _reconcile_pending_cancellations(self) -> int:
        """Settle what is due: pending cancels, lapsed leases.

        A cancel whose transfers are already idle ends at once (that costs no
        stop budget: it is the success path, and it is what a restarted
        Controller does for an intent its predecessor persisted); a cancel whose
        stop is still unconfirmed is left to the reconcile loop, which re-issues
        the stop at the core's bounded rate and, after its budget, ends it.
        """

        with self._session() as session:
            candidates = [
                (operation.id, self._payload_of(operation))
                for operation in session.scalars(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind == "download",
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                )
            ]
        settled = 0
        for operation_id, payload in candidates:
            if (
                payload is not None
                and payload.cancellation is not None
                and self.effects_settled(operation_id, payload)
            ):
                settled += int(self._try_settle_cancellation(operation_id))
        return settled + self._reconciler.reconcile().changed

    @staticmethod
    def _payload_of(
        operation: ModelCacheOperation,
    ) -> ModelCacheOperationPayload | None:
        value = _operation_payload(operation)
        return None if isinstance(value, Residue) else value  # no readable intent

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
            raise ModelCacheConflictInvalid(
                ModelCacheCode.CANCELLATION_INVALID,
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
            return False  # gone: a parent waits for nothing
        payload = self._payload_or_retire(operation)
        if payload is None:
            return False  # unreadable: retired (ended), nothing left to cancel
        existing = _operation_cancellation(operation)
        if existing is not None:
            if (existing.request_key, existing.actor, existing.reason) != (
                cancellation.request_key,
                cancellation.actor,
                cancellation.reason,
            ):
                if preserve_existing_owner:
                    return False
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.CANCELLATION_KEY_REUSED,
                    "operation already has a different cancellation request",
                )
            return False
        if (
            operation.kind != "download"
            or operation.state not in model_cache_states.LIVE
            or operation.request_key == cancellation.request_key
        ):
            raise ModelCacheConflictInvalid(
                ModelCacheCode.NOT_CANCELLABLE,
                "model download is not active or cancellation key conflicts",
            )
        _store_operation_payload(
            operation,
            operation.kind,
            payload.model_copy(
                update={"cancellation": cancellation, "failure": None, "result": None}
            ),
        )
        now = self._clock()
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "cancelling", now, waiting=True)
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
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            return self._operation_view(operation)

    def get_operator_operation(
        self, operation_id: str
    ) -> tuple[CacheOperationView, ModelCacheOperatorAction, str]:
        """Return one model operation with its action and a selector.

        A read never refuses for a missing optional fact: an operation the
        platform started itself (a recipe preparation, a repair) carries no
        operator selector, so the model's catalog name, its content digest, or
        at last the operation id stands in. All of them name the operation.
        """

        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            payload = self._payload_or_none(operation)
            selector = None if payload is None else payload.selector
            if not isinstance(selector, str) or not selector:
                model_digest = (
                    payload.model_content_sha256
                    if isinstance(payload, ModelCacheRemovalPayload)
                    else payload.manifest.model_content_sha256
                    if payload is not None
                    else None
                )
                selector = self._observed_selector(
                    session,
                    operation,
                    model_digest if isinstance(model_digest, str) else None,
                )
            action: ModelCacheOperatorAction = (
                "remove" if operation.kind == "remove" else "download"
            )
            return self._operation_view(operation), action, selector

    @staticmethod
    def _observed_selector(
        session: Session,
        operation: ModelCacheOperation,
        model_content_sha256: str | None,
    ) -> str:
        """Name a selector-less operation by its model, else its id."""

        digest = model_content_sha256
        if not isinstance(digest, str) or not digest:
            digest = session.scalar(
                select(ModelCacheSet.model_content_sha256).where(
                    ModelCacheSet.artifact_set_sha256 == operation.artifact_set_sha256
                )
            )
        if isinstance(digest, str) and digest:
            row = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.content_digest == digest,
                )
                .limit(1)
            )
            if row is not None and row.publisher and row.slug:
                return f"{row.publisher}/{row.slug}"[:256]
            return digest
        return operation.id

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
            if operation is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            operation_id = operation.id
        return self.get_operator_operation(operation_id)

    def list_operations(self, *, limit: int = 100) -> tuple[CacheOperationView, ...]:
        if not 1 <= limit <= 100:
            raise InvalidValue("cache operation limit is invalid")
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
            raise InvalidValue("cache operation limit is invalid")
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
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.CURSOR_INVALID, "operation cursor boundary is stale"
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
        # An unreadable document renders from the row's own columns: a succeeded
        # operation with its derived result, a failed one with its ``last_error``
        # as the failure evidence.  Reading never raises on a damaged envelope,
        # progress or result (a damaged ``cancellation`` sub-document still does:
        # that raise is the input-validation family's).
        stored = ModelCacheService._payload_of(operation)
        progress = _operation_progress(operation)
        cancellation = None if stored is None else stored.cancellation
        result = None if stored is None else stored.result
        failure = None if stored is None else stored.failure
        if stored is None:
            result = _derived_result(operation)
            if operation.state == State.FAILED and failure is None:
                failure = _cache_failure(
                    ModelCacheCode.DOCUMENT_UNREADABLE,
                    redact_text(
                        operation.last_error or "operation document is unreadable"
                    )[:512],
                    retryable=False,
                    recovery="inspect",
                )
        removal = stored if isinstance(stored, ModelCacheRemovalPayload) else None
        waiting = operation.state in model_cache_states.WAITING_OR_FAILED
        blockers = tuple(stored.blockers) if stored is not None and waiting else ()
        # The one retry clock is the column; a row written before the core still
        # carries it in the payload until its next transition.
        next_attempt = (
            _iso(operation.next_action_at or legacy_retry_due(operation.payload))
            if blockers and operation.state in model_cache_states.WAITING
            else None
        )
        view = CacheOperationView(
            id=operation.id,
            request_key=operation.request_key,
            kind=operation.kind,
            state=(
                LifecycleState.OBSERVING.value
                if cancellation is not None and operation.state != "cancelled"
                else model_cache_states.adopted(operation.state)
            ),
            attempt=int(operation.attempt),
            model_content_sha256=None
            if removal is None
            else removal.model_content_sha256,
            artifact_set_sha256=operation.artifact_set_sha256,
            plan_digest=operation.plan_digest,
            review_digest=None if removal is None else removal.review_digest,
            progress=serialize_json_value(progress),  # type: ignore[arg-type]
            result=result,
            last_error=operation.last_error,
            created_at=_iso(operation.created_at) or "",
            updated_at=_iso(operation.updated_at) or "",
            completed_at=_iso(operation.completed_at),
            retryable=failure is not None and failure.retryable,
            failure=None if failure is None else failure.model_dump(mode="json"),
            cancellation=(
                None if cancellation is None else cancellation.model_dump(mode="json")
            ),
            blockers=blockers,
            next_attempt_at=next_attempt if isinstance(next_attempt, str) else None,
        )
        ModelCacheOperationResponse.model_validate(view, from_attributes=True)
        return view

    @staticmethod
    def _canonical_failure(
        operation: ModelCacheOperation,
    ) -> AvailabilityOperationFailure | None:
        """Read the one current persisted failure contract without repair/defaults."""
        payload = ModelCacheService._payload_of(operation)
        return None if payload is None else payload.failure

    def resume_operations(self, *, limit: int = 16) -> int:
        """Return durable cache work for the Controller worker to resume.

        Startup must not perform network or disk transfers inline.  The
        worker calls :meth:`tick` after it has claimed its process loop, so an
        API restart only discovers outstanding work here.
        """
        if not 1 <= limit <= 100:
            raise InvalidValue("cache operation limit is invalid")
        with self._session() as session:
            count = require_integer(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind.in_(["download", "repair", "remove"])
                    )
                    .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
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
            raise InvalidValue("cache worker batch limit is invalid")
        self._reconcile_pending_cancellations()
        self._resume_after_credential_change()
        self.reconcile_requested_removals()
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
            raise InvalidValue("cache worker batch limit is invalid")
        self._streams.tick()
        self._reconcile_pending_cancellations()
        self._resume_after_credential_change()
        # Removal steps use the same Controller model-cache worker boundary,
        # but never occupy a transfer slot while waiting: each artifact lock
        # and SQL ownership check is nonblocking and a contended step is
        # durably deferred before this bounded local filesystem action returns.
        self.reconcile_requested_removals()
        removal_steps = self.advance_removals(
            limit=min(requested, self._max_parallel_downloads)
        )
        with self._lock:
            completed = self._advance_background_operations()
            capacity = self._available_transfer_slots()
            if not capacity:
                return completed + removal_steps
            claimed = self._claim_operations(
                limit=min(requested, capacity), respect_backoff=True
            )
            for operation_id, kind in claimed:
                with self._session() as session:
                    operation = session.get(ModelCacheOperation, operation_id)
                    stored = (
                        None if operation is None else self._transfer_or_none(operation)
                    )
                    refresh = stored is not None and stored.force_refresh
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
                    record = self._background_operations.get(operation_id)
                    pending = 0 if record is None else record.pending()
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
            - sum(record.pending() for record in self._background_operations.values()),
        )

    def _schedule_background_download(
        self, operation_id: str, *, force: bool, capacity: int
    ) -> None:
        started = self._start_transfer(operation_id, force=force)
        if started is None:
            return
        manifest, set_digest, force, transfer = started
        planned_total = transfer.total_bytes
        with self._session(write=True) as session:
            self._ensure_set(session, manifest)
        transfer_attempt = self._mark_running(operation_id)
        if transfer_attempt is None:
            return
        specs = list(_unique_artifacts(manifest.artifacts).values())
        self._background_operations[operation_id] = _BackgroundTransfer(
            set_digest=set_digest,
            manifest=manifest,
            force=force,
            specs=specs,
            planned_total=planned_total,
            transfer_attempt=transfer_attempt,
        )
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
        pending = record.pending()
        while pending < capacity and record.next_index < len(record.specs):
            spec = record.specs[record.next_index]
            record.next_index += 1
            future = self._executor.submit(
                self._download_one_unique,
                spec,
                record.set_digest,
                operation_id=operation_id,
                force=record.force,
                interrupt_after_bytes=None,
            )
            record.futures.append(future)
            record.future_specs[future] = spec.key
            pending += 1

    def _advance_background_operations(self) -> int:
        self._renew_background_claims()
        finished = 0
        for operation_id, record in list(self._background_operations.items()):
            futures: list[Future[None]] = record.futures
            first_error = record.failure
            future: Future[None]
            for future in [future for future in futures if future.done()]:
                futures.remove(future)
                failed_artifact_key = record.future_specs.pop(future, None)
                try:
                    future.result()
                except UnknownOutcomeError as error:
                    # An unknown outcome (a busy writer, unconfirmed storage or
                    # bookkeeping, a stopped transfer) is observed and retried,
                    # never ended: the operation keeps its durable row, settles
                    # below through the core's bounded backoff and the claim
                    # loop resumes the same transfer on a later tick.
                    if first_error is None:
                        first_error = error
                        record.failure = error
                        record.failure_artifact_key = failed_artifact_key
                    sibling: Future[None]
                    for sibling in futures:
                        sibling.cancel()
                except Exception as error:  # noqa: BLE001 - settle failed background transfers durably
                    if record.failure is None:
                        record.failure = error
                        record.failure_artifact_key = failed_artifact_key
                    other: Future[None]
                    for other in futures:
                        other.cancel()
            first_error = record.failure
            if first_error is not None:
                # A cancelled Future may still be running. Keep the durable
                # claim and record until every sibling has settled, so a
                # late checkpoint cannot resurrect a failed operation or
                # overwrite its terminal failure payload.
                if futures:
                    continue
                if isinstance(first_error, InterruptedError):
                    self._finish_partial(
                        operation_id,
                        record.set_digest,
                        record.manifest,
                        str(first_error) or "download interrupted",
                    )
                else:
                    self._finish_failed(
                        operation_id,
                        record.set_digest,
                        record.manifest,
                        first_error,
                        failed_artifact_key=record.failure_artifact_key,
                        transfer_attempt=record.transfer_attempt,
                    )
                self._background_operations.pop(operation_id, None)
                finished += 1
                continue
            if record.next_index >= len(record.specs) and not futures:
                self._finish_background_success(
                    operation_id,
                    record.set_digest,
                    record.manifest,
                    record.planned_total,
                )
                self._background_operations.pop(operation_id, None)
                finished += 1
            else:
                capacity = self._available_transfer_slots()
                self._fill_background_slots(
                    operation_id, record.pending() + min(1, capacity)
                )
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
                # An unreadable document carries no readable cancel intent: the
                # transfer keeps its lease and the claim loop reconciles the row.
                operation_payload = self._payload_or_none(operation)
                if operation.state == "cancelled" or (
                    operation_payload is not None
                    and operation_payload.cancellation is not None
                ):
                    self._transfer_stop(operation_id).set()
                    self._background_operations[
                        operation_id
                    ].failure = InterruptedError(
                        "model download cancellation was accepted"
                    )
                    continue
                # The heartbeat of every transfer this process runs: one Heartbeat
                # per operation renews its lease (a fence that is not ours is
                # left alone).
                self._lifecycle.renew(
                    operation,
                    self._claim_owner,
                    _TRANSFER_CLAIM_SECONDS,
                    now,
                    take=False,
                )

    def _finish_background_success(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        planned_total: int,
    ) -> None:
        self._set_operation_progress(
            operation_id,
            manifest,
            phase="completed",
            completed_artifacts=len(manifest.artifacts),
            downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
            expected_bytes=planned_total,
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
        self._finish_succeeded(
            operation_id,
            ModelCacheDownloadResult(
                schema_version=SCHEMA_VERSION,
                artifact_set_sha256=set_digest,
                coverage="complete",
            ),
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
                                (*model_cache_states.LIVE, "failed")
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
                    .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
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
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .with_for_update(skip_locked=True)
                    # The cooldown scan may have cached this row before another
                    # worker committed a claim; inspect the locked database value.
                    .execution_options(populate_existing=True)
                )
                if operation is None:
                    continue
                row = self._lifecycle.lifecycle(operation, now)
                if row.state is State.OBSERVING:
                    continue  # a cancel is being settled
                if (
                    row.state is State.RUNNING
                    and row.lease_deadline is not None
                    and row.lease_deadline > _aware(now)
                ):
                    # A live lease, ours or another process's (a running workload
                    # of an older or newer Controller): never retired, even when
                    # this process cannot read its document.
                    continue
                # An unreadable envelope is rebuilt from its set row, else the
                # operation ends as failed (kept for inspection) and the claim
                # loop carries on: one damaged row never stops the others.
                payload = self._transfer_or_retire(operation, now=now)
                if payload is None or payload.cancellation is not None:
                    continue
                if row.state is State.RUNNING:
                    # The attempt can no longer report: the core decides (rule 1:
                    # nothing here is irreversible, so it is retried with backoff)
                    # instead of the next claimant stealing the claim.
                    row = self._lifecycle.lapse(operation, now)
                if respect_backoff:
                    if row.next_action_at is not None and row.next_action_at > _aware(
                        now
                    ):
                        continue
                    if (
                        self._hf_cooldown_until is not None
                        and self._hf_cooldown_until > now
                        and self._payload_has_huggingface_source(payload)
                    ):
                        continue
                manifest = _manifest_of(payload)
                if has_pending_removal(
                    session,
                    (
                        ArtifactIdentity("model-set", manifest.digest),
                        *(
                            ArtifactIdentity("model-object", item.sha256)
                            for item in manifest.artifacts
                        ),
                    ),
                ):
                    self._lifecycle.settle(
                        operation,
                        Reported(
                            Outcome.UNKNOWN,
                            retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                            reason="Waiting for the prior model removal fence to settle",
                        ),
                        now,
                        consume_retry=False,
                    )
                    self._store_failure(
                        operation,
                        _cache_failure(
                            ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                            "Waiting for the prior model removal fence to settle",
                            retryable=True,
                            recovery="retry",
                            retry_time=_iso(operation.next_action_at),
                        ),
                    )
                    continue
                if self._capacity_holds(operation, payload, now):
                    continue
                if not self._lifecycle.claim(
                    operation,
                    self._claim_owner,
                    _TRANSFER_CLAIM_SECONDS,
                    now,
                    ignore_backoff=not respect_backoff,
                ):
                    continue
                claimed.append((operation.id, operation.kind))
        return claimed

    @staticmethod
    def _payload_has_huggingface_source(payload: ModelCacheOperationPayload) -> bool:
        if not isinstance(payload, ModelCacheDownloadPayload):
            return False
        for artifact in payload.manifest.artifacts:
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
            payload = self._payload_or_none(operation)
            if payload is None or not self._payload_has_huggingface_source(payload):
                continue  # unreadable: it carries no usable throttle evidence
            failure = self._canonical_failure(operation)
            if failure is None or failure.code != "rate_limited":
                continue
            retry_at = failure.retry_time
            if retry_at is None:
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
            raise ModelCacheConflictInvalid(
                ModelCacheCode.STALE_PLAN, "repair preview is stale"
            )
        request_key = _request_key(request_key)
        with self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                return self._operation_view(existing)
        manifest = self._manifest_for_set(digest)
        transfer = self._transfer_state_for_manifest(manifest, force=True)
        repair_bytes = transfer.total_bytes + _split_transient_bytes(manifest, None)
        waiting_for = ""
        if repair_bytes > self.free_bytes():
            self._request_storage(
                repair_bytes, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
            )
            waiting_for = ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
        capacity = self._capacity_wait(waiting_for)
        wait_until = None if capacity is None else capacity[1]
        payload = _write_operation_payload(
            "repair",
            ModelCacheRepairPayload(
                repair_checkpoint=ModelCacheRepairCheckpoint(
                    transfer_id=uuid.uuid4().hex, completed_objects=[]
                ),
                schema_version=SCHEMA_VERSION,
                source_policy=SOURCE_POLICY,
                artifact_set_sha256=digest,
                manifest=manifest.contract(),
                plan_digest=requested_plan,
                transfer=transfer,
                retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                failure=None if capacity is None else capacity[0],
            ),
        )
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                now = self._clock()
                self._require_model_sets_open(session, (digest,), now=now)
                operation = ModelCacheAdapter.new_operation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="repair",
                    next_action_at=wait_until,
                    attempt=1,
                    artifact_set_sha256=digest,
                    plan_digest=requested_plan,
                    payload=serialize_json_value(payload),
                    progress=progress_document(
                        self._progress(
                            manifest,
                            phase="queued",
                            expected_bytes=transfer.total_bytes,
                        )
                    ),
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _capacity_wait(
        self, waiting_for: str
    ) -> tuple[AvailabilityOperationFailure, datetime] | None:
        """Make a capacity blocker a wait on the new operation, not a refusal.

        The storage demand is already filed (the retention sweep frees space);
        the operation is queued with a retryable ``download_blocked`` failure and
        the shared retry clock.  The claim loop re-checks the space when the
        clock is due (:meth:`_capacity_holds`), so nothing is downloaded into a
        full disk and nothing is left for a person to resubmit.
        """

        if not waiting_for:
            return None
        due = self._clock() + timedelta(seconds=_RETRY_BASE_SECONDS)
        failure = _cache_failure(
            ModelCacheCode.DOWNLOAD_BLOCKED,
            waiting_for,
            retryable=True,
            recovery="capacity",
            retry_time=_iso(due),
            retry_after_seconds=_RETRY_BASE_SECONDS,
        )
        return failure, due

    def _capacity_holds(
        self,
        operation: ModelCacheOperation,
        payload: ModelCacheDownloadPayload,
        now: datetime,
    ) -> bool:
        """Whether a capacity-blocked operation still lacks the space it needs.

        If so the core reschedules it (bounded backoff, never an operator wait).
        """

        failure = payload.failure
        if failure is None or failure.code not in {
            ModelCacheCode.DOWNLOAD_BLOCKED,
            ModelCacheCode.CAPACITY,
        }:
            return False
        try:
            needed = payload.transfer.total_bytes + _split_transient_bytes(
                _manifest_of(payload), None
            )
        except (TypeError, ValueError, ModelCacheError):
            return False
        if needed <= self.free_bytes():
            return False
        self._request_storage(
            needed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
        )
        self._lifecycle.complete(
            operation,
            Reported(
                Outcome.UNKNOWN,
                retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                reason=ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE,
            ),
            now,
            interrupted=False,
            consume_retry=False,
        )
        return True

    def _manifest_for_set(self, digest: str) -> ArtifactSetManifest:
        with self._session() as session:
            row = session.get(ModelCacheSet, digest)
            # The row's key is the identity assigned at ingress (verified there,
            # never re-hashed inside the system); a stored manifest that does not
            # read is re-derived from the catalog, else the set is unknown.
            manifest = None if row is None else self._stored_manifest(row)
            if manifest is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.ENTRY_MISSING, "cache entry was not found"
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
        if manifest.model_content_sha256 is None:
            # No primary model pin: the evidence is unknown, not a refusal.
            controller_state = "unknown"
            reason = "the artifact set pins no primary model definition"
        elif controller_state in {"failed", "unknown"}:
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
            raise InvalidValue("operation provider page limit is invalid")
        if state is not None and (not isinstance(state, str) or not state.strip()):
            raise InvalidValue("operation state filter is invalid")
        if node_id is not None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        # A filter may still name a retired spelling (one release).
        named = None if state is None else input_state(state)
        if state is not None and named is None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        with self._session() as session:
            filters = []
            if named is not None:
                filters.append(
                    ModelCacheOperation.state.in_(model_cache_states.words(named))
                )
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
            self._reverify_set(manifest)
            raise ModelCacheConflictUnknown(
                ModelCacheCode.COVERAGE_INCOMPLETE,
                "cache artifact set is not completely verified; it is being "
                "verified again and the request can be retried",
                retry_after_seconds=_RETRY_BASE_SECONDS,
                recovery="reverify",
            )

    def _reverify_set(self, manifest: ArtifactSetManifest) -> None:
        """A missing receipt is unknown, not final: look again, then repair.

        The receipts beside the bytes own availability, so the set's row follows
        them: complete coverage heals a row that said otherwise; incomplete
        coverage marks the set ``needs-repair`` and queues one download of the
        missing objects (a normal download fetches only what has no receipt).
        Throttled per set: a consumer retrying every few seconds costs one scan.
        Best effort by design: the caller still gets its (retryable) refusal.
        """

        now = self._clock()
        with self._lock:
            last = self._reverified_at.get(manifest.digest)
            if last is not None and 0 <= (now - last).total_seconds() < 30:
                return
            self._reverified_at[manifest.digest] = now
        try:
            present = self._objects_present(manifest)
            if present is None:
                return
            with self._session(write=True) as session:
                row = session.get(ModelCacheSet, manifest.digest)
                if row is not None:
                    row.verified_bytes = self._verified_bytes(session, manifest.digest)
                    if present and row.state in {"needs-repair", "incomplete"}:
                        row.state = "cached"
                        row.verified_at = now
                        row.last_error = None
                    elif not present and row.state == "cached":
                        row.state = "needs-repair"
                        row.last_error = "a stored object lost its receipt"
                    row.updated_at = now
                active = session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.artifact_set_sha256 == manifest.digest,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                )
            if present or active or row is None:
                return
            preview = self._download_preview_for_manifest(manifest)
            self.start_download(
                actor=_REVERIFY_ACTOR,
                request_key=str(uuid.uuid4()),
                plan_digest=str(preview["plan_digest"]),
                artifact_set_sha256=manifest.digest,
            )
        except Exception as error:  # noqa: BLE001 - re-verification is best effort
            _LOGGER.warning(
                "re-verifying cache set %s failed: %s", manifest.digest, error
            )

    def cached_artifact_file(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
    ) -> tuple[Path, int, str]:
        """Return the requested cache object's path after identity and size checks.

        The bytes are not re-hashed: they were verified on ingress from the
        upstream and the cache is our own storage. This is the
        Controller-to-agent serving seam.  The caller receives a
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
            raise ModelCacheNotFoundRefused(
                ModelCacheCode.ARTIFACT_MISSING, "verified cache artifact was not found"
            )
        manifest = self._manifest_for_set(set_digest)
        spec = next(
            (
                value
                for value in manifest.artifacts
                if value.sha256 == object_digest and value.path == artifact_path
            ),
            None,
        )
        if spec is None:
            raise ModelCacheNotFoundRefused(
                ModelCacheCode.ARTIFACT_MISSING, "verified cache artifact was not found"
            )
        # Only this object is served, so only this object is checked. Whole-set
        # coverage is proven when the assignment is created; repeating it here
        # cost one receipt read and open per file in the set on every range
        # request, which on an NFS cache dominated the transfer rate.
        path = self._object_path(spec.sha256)
        if (
            not self._object_is_available(spec.sha256, spec.expected_bytes)
            or path.is_symlink()
            or not path.is_file()
        ):
            self._reverify_set(manifest)
            raise ModelCacheConflictUnknown(
                ModelCacheCode.ARTIFACT_UNVERIFIED,
                "cache artifact is no longer verified; it is being verified "
                "again and the request can be retried",
                retry_after_seconds=_RETRY_BASE_SECONDS,
                recovery="reverify",
            )
        return path, spec.expected_bytes, spec.sha256

    def adopt_verified_set(self, manifest: ArtifactSetManifest) -> None:
        """Record a set whose every object is already verified in storage.

        A new model or recipe revision can select files that other sets
        already cached. Their ingress receipts are the evidence: nothing is
        fetched or re-hashed, and the set becomes cached for every consumer.
        """

        with self._lock, self._session(write=True) as session:
            now = self._clock()
            self._require_model_sets_open(
                session,
                (manifest.digest,),
                now=now,
                object_digests=tuple(spec.sha256 for spec in manifest.artifacts),
            )
            row = self._ensure_set(session, manifest)
            if row.state != "cached":
                row.state = "cached"
                row.verified_bytes = manifest.expected_bytes
                row.verified_at = now
                row.updated_at = now
                row.last_accessed_at = now
                row.last_error = None

    def resolve_verified_artifact_set(
        self,
        artifact_set_sha256: str,
        *,
        manifest: ArtifactSetManifest | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Describe every verified object in a complete immutable set.

        Compilation and distribution trust durable publication receipts and
        check managed file metadata once. Serving an object separately verifies
        that object's bytes; describing a set must not scan every model file.
        No source URL or caller-controlled path is exposed by this adapter.

        The set is keyed by its bytes, so every model or recipe revision that
        selects the same files shares it, while the stored row keeps the
        provenance of whichever revision cached it first. A caller passing its
        own resolved ``manifest`` gets the same verified objects described with
        its model identities; nothing is re-hashed or downloaded.
        """
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise ModelCacheNotFoundInvalid(
                ModelCacheCode.ENTRY_MISSING, "cache entry was not found"
            )
        if manifest is not None and manifest.digest != digest:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.IDENTITY_CONFLICT,
                "requested manifest does not name this artifact set",
            )
        try:
            stored: ArtifactSetManifest | None = self._manifest_for_set(digest)
        except ModelCacheNotFound:
            if manifest is None:
                raise
            stored = None
        manifest = manifest or stored
        assert manifest is not None
        self._require_managed_cache_coverage(manifest)
        if stored is None:
            self.adopt_verified_set(manifest)
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
            raise InvalidValue("verified artifact read bounds are invalid")
        path, size, _digest = self.cached_artifact_file(
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
        entry = self._entry(digest)
        if entry is None:
            raise ModelCacheNotFoundInvalid(
                ModelCacheCode.ENTRY_MISSING, "cache entry was not found"
            )
        return entry

    def _entry(self, digest: str) -> dict[str, object] | None:
        """One entry's projection; ``None`` when the set is not (or no longer) held."""

        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                return None
            self._refresh_protection(session, row)
            # The set projection is recomputed when an entry is read rather
            # than after every object, so a partially completed operation
            # still reports exact stored bytes without making publication
            # quadratic in the set's own membership.
            row.verified_bytes = self._verified_bytes(session, row.artifact_set_sha256)
            # A manifest that does not read (and cannot be re-derived) lists no
            # artifacts: the entry still reports, and a repair or a new request
            # for the set restores its description.
            manifest = self._stored_manifest(row)
            artifacts = []
            unique_bytes = 0
            seen: set[str] = set()
            for spec in manifest.artifacts if manifest is not None else ():
                # One owner per fact: managed storage decides availability, and
                # the same descriptor check reports the stored length, so a
                # receipt alone cannot invent bytes.
                stored = self._stored_object(spec.sha256, spec.expected_bytes)
                actual = stored or 0
                state = (
                    ModelFileState.VERIFIED
                    if stored is not None
                    else ModelFileState.MISSING
                )
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
            update = (
                self._update_flags(session, row, manifest)
                if manifest is not None
                else (False, False)
            )
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
            raise InvalidValue("cache entry limit is invalid")
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
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.CURSOR_INVALID,
                    "cache inventory cursor boundary is stale",
                )
        page = rows[start : start + limit]
        entries = [
            entry
            for entry in (self._entry(row.artifact_set_sha256) for row in page)
            if entry is not None  # removed since the page was read
        ]
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
            # rewrites a lost receipt for a same-size object, and removes the receipt of
            # an object whose bytes are gone so admission cannot admit it.
            with self._session() as session:
                sets = list(session.scalars(select(ModelCacheSet)))
                expected: dict[str, ArtifactSpec] = {}
                for row in sets:
                    stored = self._stored_manifest(row)
                    if stored is None:
                        continue  # unknown set: skipped, the rest reconcile
                    for spec in stored.artifacts:
                        expected[spec.sha256] = spec
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
                if not available and metadata.st_size == spec.expected_bytes:
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
                    manifest = self._stored_manifest(row)
                    if manifest is None:
                        continue  # unknown set: skipped, the rest reconcile
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
                    RecipeInstallation.state.in_(INSTALLATION_ACTIVE),
                )
            )
            if any(
                self._recipe_references_cache(
                    session, installation.recipe_revision_id, cache_model_digests
                )
                for installation in installations
            ):
                reasons.add(CacheReferenceReason.RECIPE_INSTALLATION)
            running_revision_ids = session.scalars(
                select(RecipeInstallation.recipe_revision_id)
                .join(RecipeRun, RecipeRun.installation_id == RecipeInstallation.id)
                .where(
                    RecipeRun.state.in_(
                        [RunState.PLANNED, RunState.STARTING, RunState.RUNNING]
                    )
                )
            )
            if any(
                self._recipe_references_cache(session, revision_id, cache_model_digests)
                for revision_id in running_revision_ids
            ):
                reasons.add(CacheReferenceReason.RUNNING_MODEL)
        for profile in session.scalars(select(FleetProfile)):
            if _contains_digest(
                profile.assignments,
                row.model_content_sha256,
                row.recipe_revision_sha256,
            ) or self._profile_references_cache(session, profile, row):
                reasons.add(CacheReferenceReason.SAVED_PROFILE)
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
        try:
            # An unreadable or replaced revision is judged by the newest
            # readable revision of the same recipe, never raised to callers.
            recipe, _, _ = self._recipe_document(
                session, None, revision_id, tolerant=True
            )
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
        try:
            current_document = read_catalog_document(current)
        except CatalogRevisionContractError:
            return None, []
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
            try:
                candidate_document = read_catalog_document(candidate)
            except CatalogRevisionContractError:
                continue
            if not isinstance(candidate_document, ModelDefinition):
                continue
            if _model_lineage_signature(candidate_document) != current_signature:
                continue
            if not _same_model_artifact_identity(candidate, manifest) and (
                candidate.revision_number > current.revision_number
                or _datetime(candidate.created_at) > _datetime(current.created_at)
            ):
                candidates.append(candidate)
        # Multiple incomparable successors are deliberately exposed as
        # ambiguous; choosing one by wall-clock order would hide a catalog
        # lineage decision from operators.
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
        client = self._http or httpx2.Client(timeout=20, follow_redirects=False)
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
            if isinstance(latest, str) and re.fullmatch(r"[0-9a-f]{40,64}", latest):
                result.update(
                    latest_revision=latest,
                    status="current" if latest == revision else "update-available",
                )
            else:
                # Not an immutable revision: reported as an unknown check, never
                # raised (the status stays ``check-failed``).
                result["error_code"] = ModelCacheCode.UPSTREAM_REVISION_INVALID
        except (ModelCacheError, httpx2.HTTPError, ValueError, OSError) as error:
            # Provider failures must not hide accepted catalog updates or
            # expose signed URLs/credentials in the public response.
            result["error_code"] = getattr(
                error, "code", ModelCacheCode.UPSTREAM_CHECK_FAILED
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
                        "error_code": ModelCacheCode.UPSTREAM_CHECK_FAILED,
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
                    "error_code": ModelCacheCode.UPSTREAM_CHECK_BUDGET_EXHAUSTED,
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
            raise InvalidValue("cache update limit is invalid")
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
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.CURSOR_INVALID,
                        "cache update cursor boundary is stale",
                    )
            page = rows[start : start + limit]
            result = []
            upstream_sources: dict[str, list[tuple[str, str]]] = {}
            for row in page:
                manifest = self._stored_manifest(row)
                if manifest is None:
                    continue  # unknown set: not listed until it is re-derived
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

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register a download refused for lack of disk asks space in."""

        self._storage_demands = demands

    def _request_storage(self, new_bytes: int, reason: str) -> None:
        """Ask for the free NAS disk a refused download needs, reserve included."""

        if self._storage_demands is not None:
            self._storage_demands.request(
                NAS_MODELS,
                new_bytes + self._reserve_bytes,
                source="model-download",
                subject="",
                reason=reason,
            )

    def free_bytes(self) -> int:
        """Free NAS bytes beyond the reserve: one ``statvfs``, no object scan.

        Admission and previews need only this number. ``storage_summary`` walks
        every stored object, set and in-flight operation and is for the storage
        view, never for a read that repeats once per recipe.
        """

        return max(0, shutil.disk_usage(self._root).free - self._reserve_bytes)

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
                        ModelCacheOperation.state.in_(model_cache_states.LIVE)
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
                payload = self._transfer_or_none(operation)
                if payload is None:
                    continue  # unreadable: its objects are not counted in flight
                manifest = _manifest_of(payload)
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
        manifest = self._stored_manifest(row)
        if manifest is None:
            return  # unknown set: left as it is until it is re-derived
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
            receipt = read_stored_model(ModelCacheObjectReceipt, document)
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
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SELECTOR_INVALID, "model selector is required"
        )
    return value.strip()


def _request_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ModelCacheConflictInvalid(
            ModelCacheCode.REQUEST_KEY_INVALID, "request key is invalid"
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


def _is_allowed_github_release_redirect(value: str) -> bool:
    """Release downloads use one exact anonymous CDN authority only."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == _GITHUB_RELEASE_ASSET_HOST
        and parsed.netloc == _GITHUB_RELEASE_ASSET_HOST
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
        and not parsed.fragment
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
