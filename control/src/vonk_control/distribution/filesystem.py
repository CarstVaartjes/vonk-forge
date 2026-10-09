"""Distribution: filesystem."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    SecurityRefusalReason,
)

from ..runtime_image_preparation import (
    IMAGE_CACHE_DIRECTORY,
    FilesystemRuntimeImageStorage,
)
from .types import (
    DistributionError,
    DistributionRefused,
    DistributionUnknown,
    OpenedObject,
    _artifact_set_digest,
)


class FilesystemObjectSource:
    """Adapter for flat content-addressed Controller/NAS object storage."""

    def __init__(
        self,
        root: Path,
        *,
        maximum_bytes: int = 16 * 1024**4,
        artifact_manifests: dict[str, tuple[DistributionObject, ...]] | None = None,
        runtime_images: dict[str, str] | None = None,
    ) -> None:
        self.root = root
        self.maximum_bytes = maximum_bytes
        self.artifact_manifests = dict(artifact_manifests or {})
        self.runtime_images = dict(runtime_images or {})

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.runtime_images.get(archive_sha256) == image_digest

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        declared = self.artifact_manifests.get(artifact_set_sha256)
        if declared is None:
            return False
        expected = tuple(item for item in objects if item.kind == "model")
        return declared == expected and artifact_set_sha256 == _artifact_set_digest(
            expected
        )

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DistributionError(
                DistributionCode.OBJECT_INVALID, "object digest is invalid"
            )
        if not 1 <= expected_bytes <= self.maximum_bytes:
            raise DistributionError(
                DistributionCode.OBJECT_INVALID, "object length is invalid"
            )
        try:
            root = self.root
            root_stat = root.lstat()
            if (
                not root.is_absolute()
                or not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or root_stat.st_uid not in {0, os.geteuid()}
                or root_stat.st_mode & 0o022
            ):
                raise DistributionUnknown(
                    DistributionCode.OBJECT_UNAVAILABLE,
                    "managed distribution object root is unsafe",
                )
            descriptor = os.open(
                os.fspath(root),
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                fd = os.open(
                    digest,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            finally:
                os.close(descriptor)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or before.st_size != expected_bytes
            ):
                os.close(fd)
                raise DistributionUnknown(
                    DistributionCode.OBJECT_UNAVAILABLE, "stored object length changed"
                )
            # The name is the digest and the object entered the store through an
            # ingress that verified it; serving checks identity and size only.
            source = os.fdopen(fd, "rb", closefd=True)
            return OpenedObject(source, expected_bytes, digest, root / digest)
        except DistributionError:
            raise
        except PermissionError as error:
            raise DistributionRefused(
                SecurityRefusalReason.PERMISSION_DENIED,
                "managed distribution object access was denied",
                reason=SecurityRefusalReason.PERMISSION_DENIED,
            ) from error
        except OSError as error:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "stored object is unavailable"
            ) from error


class RecipeBuildObjectSource(FilesystemObjectSource):
    """Verified OCI source backed by succeeded Controller recipe builds."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifact_root: Path,
        *,
        maximum_bytes: int = 16 * 1024**4,
        artifact_manifests: dict[str, tuple[DistributionObject, ...]] | None = None,
        runtime_images: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            artifact_root / IMAGE_CACHE_DIRECTORY,
            maximum_bytes=maximum_bytes,
            artifact_manifests=artifact_manifests,
            runtime_images=runtime_images,
        )
        self.sessions = sessions
        # The runtime-image cache whose receipts prove the stored archives.
        self._runtime_storage = FilesystemRuntimeImageStorage(artifact_root)

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        return False

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        from ..oci_image_store import StoreUnknown

        stored = self._runtime_storage.layout.read(f"sha256:{archive_sha256}")
        return (
            stored is not None
            and not isinstance(stored, StoreUnknown)
            and stored.manifest_digest == image_digest
        )

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        raise DistributionUnknown(
            DistributionCode.OBJECT_UNAVAILABLE,
            "runtime images are pulled from the layered store, not downloaded",
        )
