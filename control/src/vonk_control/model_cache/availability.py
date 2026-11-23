"""Availability."""

from __future__ import annotations

import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.reason_codes.model_cache import ModelCacheCode

from ..bounded_retry import bounded_attempts
from ..categorized_errors import InvalidValue
from ..models.cache import ModelCacheSet
from .artifacts import ArtifactSetManifest, _optional_digest
from .constants import _LOGGER, _RETRY_BASE_SECONDS, _REVERIFY_ACTOR, SCHEMA_VERSION
from .contract import ModelCacheSetState
from .errors import (
    ModelCacheConflictUnknown,
    ModelCacheNotFound,
    ModelCacheNotFoundInvalid,
)
from .source_helpers import _valid_relative_path

if TYPE_CHECKING:
    from .service import ModelCacheService


class AvailabilityMixin:
    """Availability behavior of the cache service."""

    def _managed_cache_covers(self, manifest: ArtifactSetManifest) -> bool:
        """Whether managed storage holds a verified copy of every object."""
        cache = cast("ModelCacheService", self)
        return cache._managed_cached_objects(manifest) == frozenset(
            spec.sha256 for spec in manifest.artifacts
        )

    def _require_managed_cache_coverage(self, manifest: ArtifactSetManifest) -> None:
        cache = cast("ModelCacheService", self)
        if not cache._managed_cache_covers(manifest):
            cache._reverify_set(manifest)
            raise ModelCacheConflictUnknown(
                ModelCacheCode.COVERAGE_INCOMPLETE,
                "cache artifact set is not completely verified; it is being "
                "verified again and the request can be retried",
                retry_after_seconds=_RETRY_BASE_SECONDS,
                recovery="reverify",
            )

    def _reverify_set(self, manifest: ArtifactSetManifest) -> None:
        """A missing object is unknown, not final: look again, then repair.

        The stored objects own availability, so the set's row follows them:
        complete coverage heals a row that said otherwise; incomplete coverage
        marks the set ``needs-repair`` and queues one download of the missing
        objects (a normal download fetches only what is not stored).
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
            if present is None:  # pyright: ignore[reportUnnecessaryComparison]  # test doubles of _objects_present return None
                return
            with cache._session(write=True) as session:
                row = session.get(ModelCacheSet, manifest.digest)
                if row is not None:
                    row.verified_bytes = cache._verified_bytes(manifest.digest)
                    if present and row.state in {
                        ModelCacheSetState.NEEDS_REPAIR,
                        ModelCacheSetState.INCOMPLETE,
                    }:
                        row.state = ModelCacheSetState.CACHED
                        row.verified_at = now
                        row.last_error = None
                    elif not present and row.state == ModelCacheSetState.CACHED:
                        row.state = ModelCacheSetState.NEEDS_REPAIR
                        row.last_error = (
                            "a stored object is missing or has the wrong size"
                        )
                    row.updated_at = now
            active = cache.reusable_operation(
                artifact_set_sha256=manifest.digest, include_succeeded=False
            )
            if present or active or row is None:
                return
            cache.start_download(
                actor=_REVERIFY_ACTOR,
                request_key=str(uuid.uuid4()),
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
            raise ModelCacheNotFoundInvalid(
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
        # cost one open per file in the set on every range
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

    def adopt_covered_set(self, manifest: ArtifactSetManifest) -> None:
        """Record a resolved set whose every object the NAS already holds.

        A copy to a Spark reads a set by its digest, so a new set over bytes
        other sets cached is recorded before that copy. A set the NAS lacks is
        left alone: no refusal and no download, because only a copy to a Spark
        that lacks the files needs the NAS bytes, and that copy checks them.
        """
        cache = cast("ModelCacheService", self)
        with suppress(ModelCacheNotFound):
            cache._manifest_for_set(manifest.digest)
            return
        if cache._managed_cache_covers(manifest):
            cache.adopt_verified_set(manifest)

    def adopt_verified_set(self, manifest: ArtifactSetManifest) -> None:
        """Record a set whose every object is already verified in storage.

        A new model or recipe revision can select files that other sets
        already cached. Their ingress verification is the evidence: nothing is
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
            cache._remember_verified_models(session, row, manifest)
            if row.state != ModelCacheSetState.CACHED:
                row.state = ModelCacheSetState.CACHED
                row.verified_bytes = manifest.expected_bytes
                row.verified_at = now
                row.updated_at = now
                row.last_accessed_at = now
                row.last_error = None

    def resolve_verified_artifact_set(
        self, artifact_set_sha256: str
    ) -> tuple[dict[str, object], ...]:
        """Describe every verified object in a complete immutable set.

        Distribution trusts ingress-verified objects and checks managed
        file metadata once. Serving an object separately verifies that
        object's bytes; describing a set must not scan every model file. No
        source URL or caller-controlled path is exposed by this adapter.
        Compilation needs no NAS copy and records a covered set through
        ``adopt_covered_set`` instead.
        """
        cache = cast("ModelCacheService", self)
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise ModelCacheNotFoundInvalid(
                ModelCacheCode.ENTRY_MISSING, "cache entry was not found"
            )
        manifest = cache._manifest_for_set(digest)
        cache._require_managed_cache_coverage(manifest)
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
        offset: object = 0,
        maximum_bytes: object = 8 * 1024 * 1024,
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
