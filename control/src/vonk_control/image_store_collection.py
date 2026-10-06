"""Collect runtime image bytes the Controller no longer names.

The layered store keeps an image's blobs while something still names it:
- its receipt in ``image-cache`` (a published, reusable image);
- a live distribution assignment (Sparks may still be pulling it);
- a build that is not finished, or finished within the grace period (a
  stored image waiting for its first receipt).

Everything else is proven unreferenced. A failed scan proves nothing: when
the receipts, an image's manifest or the database cannot be read, the pass
deletes nothing, names the reason in the log and runs again next interval.
Unreferenced blobs go once they are older than the grace period, and a later request that needs the image again stores or
builds it again, as for any cache loss. A Spark build's uploaded archive that
no build still waits on is removed the same way.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import ImageStoreCode

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    lock_reference_gates,
)
from .artifact_reference_scan import runtime_image_reference_findings
from .logging import log_event
from .models import ArtifactDistributionAssignment, RecipeBuild
from .oci_image_store import (
    IMAGE_CACHE_DIRECTORY,
    REFERENCE_SCAN_FAILED,
    REFERENCED_MANIFEST_DAMAGED,
    Collection,
    OciImageStore,
    OciImageStoreError,
)
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)

_LOGGER = logging.getLogger(__name__)
_ADDRESS = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT = re.compile(r"^([0-9a-f]{64})\.receipt\.json$")
_UNFINISHED_BUILD_STATES = ("planned", "building")
# Damaged receipted images evicted by one pass; the rest follow next interval.
_MAX_REPAIRS = 64
GRACE = timedelta(hours=24)
INTERVAL = timedelta(hours=1)


class ImageStoreCollector:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifact_root: Path,
        *,
        clock: Callable[[], datetime],
        store: OciImageStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._image_cache = artifact_root / IMAGE_CACHE_DIRECTORY
        self._store = store or OciImageStore(artifact_root)
        self._images = FilesystemRuntimeImageStorage(artifact_root)
        self._clock = clock
        self._due_at: datetime | None = None

    def tick(self) -> bool:
        """Collect at most once per interval; never waits for the store."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        result = self.collect()
        return result is not None and result.blobs_removed > 0

    def collect(self) -> Collection | None:
        collection: Collection | None = None
        for _attempt in range(_MAX_REPAIRS):
            try:
                collection = self._store.collect(
                    self._referenced_images, grace_seconds=GRACE.total_seconds()
                )
                break
            except OciImageStoreError as error:
                if error.code not in (
                    REFERENCE_SCAN_FAILED,
                    REFERENCED_MANIFEST_DAMAGED,
                ):
                    raise
                # Nothing was removed: what the Controller still names is unknown.
                _log_deferred(error.code, error.detail)
                if error.code != REFERENCED_MANIFEST_DAMAGED or not (
                    error.address and self._evict_damaged_receipt(error.address)
                ):
                    break
                # The damaged image no longer holds the sweep back: go again.
        uploads = self._collect_uploads()
        if collection is None:
            return None
        result = Collection(
            collection.blobs_removed + uploads.blobs_removed,
            collection.bytes_reclaimed + uploads.bytes_reclaimed,
        )
        if result.blobs_removed:
            _LOGGER.info(
                "reclaimed %d unreferenced runtime image files (%d bytes)",
                result.blobs_removed,
                result.bytes_reclaimed,
            )
        return result

    def _evict_damaged_receipt(self, address: str) -> bool:
        """Retire the receipt of an image whose manifest is damaged.

        A receipt names an image the store can no longer serve: it is derived
        metadata, and the next request prepares the image again (a damaged
        manifest reads as cache loss), so it must not hold back cleanup of the
        whole store forever. Fenced like every receipt removal: nothing is
        removed while an operation works on the image or another removal owns
        it; that is retried next interval.
        """

        receipt = self._image_cache / f"{address}.receipt.json"
        try:
            with self._images.publication_lock(address):
                if not receipt.exists():
                    return False
                with self._sessions.begin() as session:
                    rows = lock_reference_gates(
                        session,
                        (ArtifactIdentity("runtime-image", address),),
                        now=self._clock(),
                    )
                    if any(row.removal_owner_id is not None for row in rows):
                        return False
                    findings = runtime_image_reference_findings(session, (address,))
                    if any(
                        finding.classification == "active-work"
                        for finding in findings.get(address, ())
                    ):
                        return False
                    self._images.remove_published(address)
        except (RuntimeImagePreparationError, ArtifactLifecycleError, OSError) as error:
            _log_deferred(
                getattr(error, "code", type(error).__name__),
                f"damaged image {address} was not evicted: {error}",
            )
            return False
        log_event(
            _LOGGER,
            ImageStoreCode.DAMAGED_RECEIPT_EVICTED,
            service="control-worker",
            address=address,
        )
        return True

    def _referenced_images(self) -> set[str]:
        referenced = self._receipt_addresses()
        now = self._clock()
        with self._sessions() as session:
            referenced.update(
                session.scalars(
                    select(ArtifactDistributionAssignment.oci_archive_sha256).where(
                        ArtifactDistributionAssignment.state == "active",
                        ArtifactDistributionAssignment.expires_at > now,
                    )
                )
            )
            referenced.update(self._waiting_builds(session, now))
        return referenced

    def _receipt_addresses(self) -> set[str]:
        """Images with a receipt. An unreadable directory is an error, not none.

        ``Path.glob`` silently yields nothing for a directory it may not list,
        which would read as "no image is published".
        """

        try:
            with os.scandir(self._image_cache) as entries:
                names = [entry.name for entry in entries]
        except FileNotFoundError:
            return set()
        except OSError as error:
            raise OciImageStoreError(
                REFERENCE_SCAN_FAILED,
                f"image receipts cannot be listed, so nothing was removed: "
                f"{type(error).__name__}: {error}",
            ) from error
        return {
            match.group(1)
            for name in names
            if (match := _RECEIPT.fullmatch(name)) is not None
        }

    @staticmethod
    def _waiting_builds(session: Session, now: datetime) -> set[str]:
        rows = session.execute(
            select(
                RecipeBuild.oci_layout_sha256,
                RecipeBuild.state,
                RecipeBuild.updated_at,
            ).where(RecipeBuild.oci_layout_sha256.is_not(None))
        )
        recent = now - GRACE
        return {
            str(address)
            for address, state, updated_at in rows
            if state in _UNFINISHED_BUILD_STATES
            or (state == "succeeded" and _aware(updated_at, now) >= recent)
        }

    def _collect_uploads(self) -> Collection:
        """Remove uploaded archives no unconverted build waits on."""

        if not self._image_cache.is_dir():
            return Collection(0, 0)
        with self._sessions() as session:
            waiting = set(
                session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        RecipeBuild.state.in_(("building", "succeeded"))
                    )
                )
            )
        cutoff = time.time() - GRACE.total_seconds()
        removed = reclaimed = 0
        try:
            uploads = list(self._image_cache.iterdir())
        except OSError as error:
            _log_deferred(
                REFERENCE_SCAN_FAILED,
                f"uploaded archives cannot be listed, so none was removed: "
                f"{type(error).__name__}: {error}",
            )
            return Collection(0, 0)
        for upload in uploads:
            if _ADDRESS.fullmatch(upload.name) is None or upload.name in waiting:
                continue
            try:
                status = upload.lstat()
                if not upload.is_file() or status.st_mtime > cutoff:
                    continue
                upload.unlink()
            except FileNotFoundError:
                continue
            removed += 1
            reclaimed += status.st_size
        return Collection(removed, reclaimed)


def _log_deferred(code: str, detail: str) -> None:
    log_event(
        _LOGGER,
        ImageStoreCode.COLLECTION_DEFERRED,
        service="control-worker",
        code=code,
        detail=detail,
    )


def _aware(value: datetime, reference: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


__all__ = ["GRACE", "INTERVAL", "ImageStoreCollector"]
