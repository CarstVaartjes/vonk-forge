"""Availability."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from vonk_agent_protocol import ModelCacheCode, WaitReason

from .. import model_cache_states
from ..bounded_retry import bounded_attempts
from ..categorized_errors import InvalidValue
from ..models import ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSetManifest, _optional_digest
from .constants import _LOGGER, _RETRY_BASE_SECONDS, _REVERIFY_ACTOR, SCHEMA_VERSION
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictUnknown,
    ModelCacheNotFound,
    ModelCacheNotFoundInvalid,
    ModelCacheNotFoundRefused,
)
from .source_helpers import _valid_relative_path

if TYPE_CHECKING:
    from .service import ModelCacheService


class AvailabilityMixin:
    """Availability behavior of the cache service."""

    def _require_managed_cache_coverage(self, manifest: ArtifactSetManifest) -> None:
        cache = cast("ModelCacheService", self)
        if cache._managed_cached_objects(manifest) != frozenset(
            spec.sha256 for spec in manifest.artifacts
        ):
            cache._reverify_set(manifest)
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
        cache = cast("ModelCacheService", self)

        now = cache._clock()
        with cache._lock:
            last = cache._reverified_at.get(manifest.digest)
            if last is not None and 0 <= (now - last).total_seconds() < 30:
                return
            cache._reverified_at[manifest.digest] = now
        try:
            present = cache._objects_present(manifest)
            if present is None:
                return
            with cache._session(write=True) as session:
                row = session.get(ModelCacheSet, manifest.digest)
                if row is not None:
                    row.verified_bytes = cache._verified_bytes(session, manifest.digest)
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
            preview = cache._download_preview_for_manifest(manifest)
            cache.start_download(
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
        """Observe the exact request again within a bounded request retry budget."""
        cache = cast("ModelCacheService", self)
        refused: ModelCacheConflictUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return cache.cached_artifact_file_once(
                    artifact_set_sha256, artifact_sha256, artifact_path
                )
            except ModelCacheConflictUnknown as error:
                refused = error
        assert refused is not None
        raise refused

    def cached_artifact_file_once(
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
        cache = cast("ModelCacheService", self)
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
        manifest = cache._manifest_for_set(set_digest)
        spec = next(
            (
                value
                for value in manifest.artifacts
                if value.sha256 == object_digest and value.path == artifact_path
            ),
            None,
        )
        if spec is None:
            raise ModelCacheConflictUnknown(
                ModelCacheCode.ARTIFACT_UNVERIFIED,
                "requested content is absent from the local cache manifest",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                retry_after_seconds=_RETRY_BASE_SECONDS,
            )
        # Only this object is served, so only this object is checked. Whole-set
        # coverage is proven when the assignment is created; repeating it here
        # cost one receipt read and open per file in the set on every range
        # request, which on an NFS cache dominated the transfer rate.
        path = cache._object_path(spec.sha256)
        if (
            not cache._object_is_available(spec.sha256, spec.expected_bytes)
            or path.is_symlink()
            or not path.is_file()
        ):
            cache._reverify_set(manifest)
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
        cache = cast("ModelCacheService", self)

        with cache._lock, cache._session(write=True) as session:
            now = cache._clock()
            cache._require_model_sets_open(
                session,
                (manifest.digest,),
                now=now,
                object_digests=tuple(spec.sha256 for spec in manifest.artifacts),
            )
            row = cache._ensure_set(session, manifest)
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
        cache = cast("ModelCacheService", self)
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
            stored: ArtifactSetManifest | None = cache._manifest_for_set(digest)
        except ModelCacheNotFound:
            if manifest is None:
                raise
            stored = None
        manifest = manifest or stored
        assert manifest is not None
        cache._require_managed_cache_coverage(manifest)
        if stored is None:
            cache.adopt_verified_set(manifest)
        descriptors = []
        for spec in manifest.artifacts:
            path = cache._object_path(spec.sha256)
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
                    "storage_key": cache._object_key(object_digest),
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
        cache = cast("ModelCacheService", self)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(maximum_bytes, int)
            or isinstance(maximum_bytes, bool)
            or not 0 < maximum_bytes <= 8 * 1024 * 1024
        ):
            raise InvalidValue("verified artifact read bounds are invalid")
        path, size, _digest = cache.cached_artifact_file(
            artifact_set_sha256, artifact_sha256, artifact_path
        )
        if offset >= size:
            return b""
        with path.open("rb") as source:
            source.seek(offset)
            return source.read(min(maximum_bytes, size - offset))
