"""Removal review 2."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, cast

from vonk_agent_protocol import AssetAvailability

from ..cache_removal_review import AssetDisposition, CacheRemovalAsset
from ..model_cache_ranges import range_partial_bytes
from ..models import ModelCacheSet
from . import constants
from .artifacts import _unique_artifacts
from .constants import _PARALLEL_RANGE_WORKERS
from .views import ModelCacheRemovalScope

if TYPE_CHECKING:
    from .service import ModelCacheService


class RemovalScopeMixin:
    """Removal scope behavior of the cache service."""

    def removal_asset_status(
        self, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalAsset, ...]:
        """Project exact model removal identities and managed-storage status.

        Manifest and membership reads happen in a short SQL context. That
        context closes before receipt or filesystem observations begin.
        """
        cache = cast("ModelCacheService", self)

        expected_by_set: dict[str, dict[str, int]] = {}
        expected_by_object: dict[str, int] = {}
        memberships_by_set = dict(scope.memberships)
        with cache._session() as session:
            for set_digest in scope.selected_sets:
                row = session.get(ModelCacheSet, set_digest)
                manifest = None if row is None else cache._stored_manifest(row)
                if manifest is None:
                    # Destructive guard: without a readable manifest the exact
                    # objects of the scope are unknown, so nothing is removed.
                    return _unknown_removal_assets(scope)
                specs = _unique_artifacts(manifest.artifacts)
                expected = {
                    digest: spec.expected_bytes for digest, spec in specs.items()
                }
                if set(expected) != set(memberships_by_set.get(set_digest, ())):
                    return _unknown_removal_assets(scope)
                for digest, expected_bytes in expected.items():
                    previous = expected_by_object.setdefault(digest, expected_bytes)
                    if previous != expected_bytes:
                        return _unknown_removal_assets(scope)
                expected_by_set[set_digest] = expected

        object_status: dict[str, tuple[AssetAvailability, int | None]] = {}
        for digest, expected_bytes in sorted(expected_by_object.items()):
            try:
                verified_bytes = cache._stored_object(digest, expected_bytes)
            except OSError:
                object_status[digest] = (AssetAvailability.UNKNOWN, None)
                continue
            if verified_bytes is not None:
                object_status[digest] = (AssetAvailability.VERIFIED, verified_bytes)
                continue
            try:
                metadata = cache._object_path(digest).lstat()
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
            partial = cache._partial_path(set_digest, digest)
            if expected_bytes >= constants._PARALLEL_RANGE_MIN_BYTES:
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
                return _unknown_removal_assets(scope)
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


def _unknown_removal_assets(
    scope: ModelCacheRemovalScope,
) -> tuple[CacheRemovalAsset, ...]:
    """Project incomplete local membership without inventing lengths or bytes."""
    sets = tuple(
        CacheRemovalAsset(
            kind="model-set",
            sha256=digest,
            availability=AssetAvailability.UNKNOWN,
            disposition="remove",
        )
        for digest in scope.selected_sets
    )
    objects = tuple(
        CacheRemovalAsset(
            kind="model-object",
            sha256=digest,
            availability=AssetAvailability.UNKNOWN,
            disposition="remove" if digest in scope.delete_objects else "retain-shared",
        )
        for digest in scope.selected_objects
    )
    return (*sets, *objects)
