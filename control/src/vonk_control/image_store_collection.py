"""Collect runtime image bytes the Controller no longer names.

The layered store keeps an image's blobs while something still names it:
- its receipt in ``image-cache`` (a published, reusable image);
- a live distribution assignment (Sparks may still be pulling it);
- a build that is not finished, or finished within the grace period (a
  stored image waiting for its first receipt).

Everything else is proven unreferenced. Its blobs go once they are older than
the grace period, and a later request that needs the image again stores or
builds it again, as for any cache loss. A Spark build's uploaded archive that
no build still waits on is removed the same way.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import ArtifactDistributionAssignment, RecipeBuild
from .oci_image_store import IMAGE_CACHE_DIRECTORY, Collection, OciImageStore

_LOGGER = logging.getLogger(__name__)
_ADDRESS = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT = re.compile(r"^([0-9a-f]{64})\.receipt\.json$")
_UNFINISHED_BUILD_STATES = ("planned", "building")
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
        collection = self._store.collect(
            self._referenced_images, grace_seconds=GRACE.total_seconds()
        )
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

    def _referenced_images(self) -> set[str]:
        referenced = {
            match.group(1)
            for path in self._image_cache.glob("*.receipt.json")
            if (match := _RECEIPT.fullmatch(path.name)) is not None
        }
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
        for upload in self._image_cache.iterdir():
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


def _aware(value: datetime, reference: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


__all__ = ["GRACE", "INTERVAL", "ImageStoreCollector"]
