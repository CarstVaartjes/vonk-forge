"""Controller-authorized delivery of immutable model and OCI objects.

The service is intentionally backed by a small source protocol.  The NAS cache
worker can provide that protocol without this module knowing its persistence or
eviction details; recipe image storage can use the filesystem adapter below.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionAssignment,
    DistributionObject,
    canonical_message,
)

from .models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    RecipeBuild,
    RuntimeImageReceipt,
)
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)


class DistributionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    """A source handle that has been checked against its content address."""

    stream: BinaryIO
    size: int
    sha256: str


class VerifiedObjectSource(Protocol):
    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        """Open a complete immutable object or raise DistributionError."""

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        """Prove that the exact model objects belong to the cache manifest."""

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        """Prove the archive is the exact OCI image selected by the plan."""


def artifact_set_sha256(objects: tuple[DistributionObject, ...]) -> str:
    """Return the digest used by cache adapters for an exact model manifest."""
    model_objects = [item.to_mapping() for item in objects if item.kind == "model"]
    return hashlib.sha256(canonical_message(model_objects)).hexdigest()


_artifact_set_digest = artifact_set_sha256


class FilesystemVerifiedObjectSource:
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
        return declared == expected and artifact_set_sha256 == _artifact_set_digest(expected)

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DistributionError("distribution.object_invalid", "object digest is invalid")
        if not 1 <= expected_bytes <= self.maximum_bytes:
            raise DistributionError("distribution.object_invalid", "object length is invalid")
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
                raise OSError("unsafe object root")
            descriptor = os.open(os.fspath(root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                fd = os.open(digest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
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
                raise DistributionError("distribution.object_unavailable", "verified object length changed")
            hasher = hashlib.sha256()
            remaining = expected_bytes
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    os.close(fd)
                    raise DistributionError("distribution.object_unavailable", "verified object is partial")
                hasher.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if after.st_size != before.st_size or hasher.hexdigest() != digest:
                os.close(fd)
                raise DistributionError("distribution.object_unavailable", "verified object digest mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            return VerifiedObject(os.fdopen(fd, "rb", closefd=True), expected_bytes, digest)
        except DistributionError:
            raise
        except OSError as error:
            raise DistributionError("distribution.object_unavailable", "verified object is unavailable") from error


class RecipeBuildVerifiedObjectSource(FilesystemVerifiedObjectSource):
    """Verified OCI source backed by succeeded Controller recipe builds."""

    def __init__(self, sessions: sessionmaker[Session], artifact_root: Path, **kwargs: object) -> None:
        super().__init__(artifact_root, **kwargs)
        self.sessions = sessions

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        return False

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(RecipeBuild.id).where(
                RecipeBuild.state == "succeeded",
                RecipeBuild.image_digest == image_digest,
                RecipeBuild.oci_layout_sha256 == archive_sha256,
                RecipeBuild.image_bytes > 0,
            )) is not None

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        with self.sessions() as session:
            authorized = session.scalar(select(RecipeBuild.id).where(
                RecipeBuild.state == "succeeded",
                RecipeBuild.oci_layout_sha256 == digest,
                RecipeBuild.image_bytes == expected_bytes,
            ))
        if authorized is None:
            raise DistributionError("distribution.object_unavailable", "OCI archive is not a succeeded build artifact")
        return super().open_verified(digest, expected_bytes)


class ControllerRuntimeImageVerifiedObjectSource(RecipeBuildVerifiedObjectSource):
    """Verified OCI source for pulled and Controller-built receipts.

    A pulled image receipt is accepted only when its recipe provenance still
    resolves to the active canonical recipe and the recipe's immutable image
    pin matches the receipt's registry identity.  Source-built images retain
    the existing succeeded ``RecipeBuild`` authority.  The filesystem is
    therefore a content store, while the database remains the authority that
    authorizes use of an archive in a target assignment.
    """

    def __init__(self, sessions: sessionmaker[Session], artifact_root: Path, **kwargs: object) -> None:
        super().__init__(sessions, artifact_root, **kwargs)
        self._runtime_storage = FilesystemRuntimeImageStorage(artifact_root)

    def _published_receipt_authorizes(self, image_digest: str, archive_sha256: str) -> bool:
        try:
            receipt = self._runtime_storage.read_receipt(archive_sha256)
            if receipt.source != "published" or receipt.image_digest != image_digest:
                return False
            if receipt.registry_manifest_digest is None:
                return False
            with self.sessions() as session:
                revision = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest
                        == receipt.distribution_content_sha256,
                        CatalogDocumentRevision.publisher
                        == receipt.distribution_publisher,
                        CatalogDocumentRevision.slug == receipt.distribution_slug,
                    )
                )
                if revision is None:
                    return False
                execution = revision.document.get("execution")
                image = execution.get("image") if isinstance(execution, Mapping) else None
                raw_registry = image.get("digest") if isinstance(image, Mapping) else None
                expected_registry = (
                    raw_registry
                    if isinstance(raw_registry, str) and raw_registry.startswith("sha256:")
                    else f"sha256:{raw_registry}"
                    if isinstance(raw_registry, str)
                    else None
                )
                if expected_registry != receipt.registry_manifest_digest:
                    return False
                durable = session.scalar(
                    select(RuntimeImageReceipt).where(
                        RuntimeImageReceipt.recipe_revision_id == revision.id,
                        RuntimeImageReceipt.source == "published",
                        RuntimeImageReceipt.original_content_digest
                        == receipt.distribution_content_sha256,
                        RuntimeImageReceipt.state == "verified",
                        RuntimeImageReceipt.registry_manifest_digest
                        == receipt.registry_manifest_digest,
                        RuntimeImageReceipt.platform_manifest_digest == image_digest,
                        RuntimeImageReceipt.local_image_config_id
                        == receipt.local_image_config_id,
                        RuntimeImageReceipt.oci_archive_sha256 == archive_sha256,
                        RuntimeImageReceipt.image_bytes == receipt.image_bytes,
                        RuntimeImageReceipt.architecture == receipt.architecture,
                        RuntimeImageReceipt.runtime_interface == receipt.runtime_interface,
                        RuntimeImageReceipt.runtime_interface_label
                        == receipt.runtime_interface_label,
                    )
                )
                if durable is None:
                    return False
            self._runtime_storage.verify_existing(archive_sha256, receipt.image_bytes)
            return True
        except (RuntimeImagePreparationError, OSError, ValueError):
            return False

    def _published_recipe_requests(self, image_digest: str) -> bool:
        """Return whether an active Recipe claims this digest as a published image."""

        with self.sessions() as session:
            revisions = session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            for revision in revisions:
                execution = revision.document.get("execution")
                image = execution.get("image") if isinstance(execution, Mapping) else None
                raw_digest = image.get("digest") if isinstance(image, Mapping) else None
                expected = (
                    raw_digest
                    if isinstance(raw_digest, str) and raw_digest.startswith("sha256:")
                    else f"sha256:{raw_digest}"
                    if isinstance(raw_digest, str)
                    else None
                )
                if expected == image_digest:
                    return True
        return False

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        if self._published_receipt_authorizes(image_digest, archive_sha256):
            return True
        # A canonical published-image request must never fall through to a
        # coincidentally matching RecipeBuild archive after its own receipt or
        # SQL authority fails.  Build fallback remains valid only for digests
        # no active published Recipe claims.
        if self._published_recipe_requests(image_digest):
            return False
        return super().verify_runtime_image(image_digest, archive_sha256)

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        if self._published_archive_authorizes(digest, expected_bytes):
            return FilesystemVerifiedObjectSource.open_verified(self, digest, expected_bytes)
        return super().open_verified(digest, expected_bytes)

    def _published_archive_authorizes(self, digest: str, expected_bytes: int) -> bool:
        try:
            receipt = self._runtime_storage.read_receipt(digest)
            if receipt.source != "published" or receipt.image_bytes != expected_bytes:
                return False
            return self._published_receipt_authorizes(receipt.image_digest, digest)
        except (RuntimeImagePreparationError, OSError, ValueError):
            return False


class ModelCacheVerifiedObjectSource:
    """Narrow adapter for the NAS model-cache verified-object service.

    ``manifests`` is keyed by the cache service's own artifact-set digest and
    contains the complete ordered model manifest.  This adapter never derives
    or rewrites that digest; the cache worker remains its authority.
    """

    def __init__(
        self,
        open_verified: Callable[[str, int], VerifiedObject],
        manifests: dict[str, tuple[DistributionObject, ...]],
    ) -> None:
        self._open_verified = open_verified
        self._manifests = dict(manifests)
        self._receipts: dict[str, tuple[dict[str, object], ...]] = {}

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        return self._open_verified(digest, expected_bytes)

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return False

    @classmethod
    def from_service(cls, service: object) -> ModelCacheVerifiedObjectSource:
        """Construct directly from the NAS worker's verified-object service."""
        return cls._from_cache_service(service)

    @classmethod
    def _from_cache_service(cls, service: object) -> ModelCacheVerifiedObjectSource:
        adapter = cls.__new__(cls)
        adapter._service = service
        adapter._manifests = {}
        adapter._receipts = {}
        adapter._paths = {}
        adapter._open_verified = adapter._open_cache_object
        return adapter

    def _load_manifest(self, digest: str) -> tuple[DistributionObject, ...]:
        try:
            # ModelCacheService validates its opaque digest against the full
            # canonical ArtifactSetManifest before exposing descriptors.
            manifest = self._service.manifest_for_artifact_set(digest)
            if manifest.digest != digest:
                raise ValueError("cache manifest identity changed")
            descriptors = self._service.resolve_verified_artifact_set(digest)
        except Exception as error:
            raise DistributionError("distribution.model_set_mismatch", "NAS cache manifest is unavailable") from error
        objects = []
        receipts = []
        for descriptor in descriptors:
            try:
                item = DistributionObject(
                    name=str(descriptor["path"]),
                    sha256=str(descriptor["sha256"]),
                    bytes=int(descriptor["bytes"]),
                    kind="model",
                )
                item = DistributionObject.parse(item.to_mapping())
                path = descriptor["file"]
            except (KeyError, TypeError, ValueError) as error:
                raise DistributionError("distribution.model_set_mismatch", "NAS cache manifest is malformed") from error
            objects.append(item)
            self._paths[item.sha256] = (digest, item.name, path)
            file_id = descriptor.get("file_id")
            model_content_sha256 = descriptor.get("model_content_sha256")
            roles = descriptor.get("roles")
            if isinstance(file_id, str) and isinstance(model_content_sha256, str):
                receipts.append(
                    {
                        "model_content_sha256": model_content_sha256,
                        "file_id": file_id,
                        "path": item.name,
                        "sha256": item.sha256,
                        "bytes": item.bytes,
                        "roles": list(roles) if isinstance(roles, list) else [],
                        "distribution_object": item.to_mapping(),
                    }
                )
        result = tuple(objects)
        self._manifests[digest] = result
        if len(receipts) == len(result):
            self._receipts[digest] = tuple(receipts)
        return result

    def _open_cache_object(self, digest: str, expected_bytes: int) -> VerifiedObject:
        entry = self._paths.get(digest)
        if entry is None:
            raise DistributionError("distribution.object_unavailable", "NAS cache object was not authorized")
        set_digest, path, _ = entry
        try:
            verified_path, size, verified_digest = self._service.verified_artifact_file(
                set_digest, digest, path
            )
            if size != expected_bytes or verified_digest != digest:
                raise DistributionError("distribution.object_unavailable", "NAS cache object identity changed")
            return VerifiedObject(verified_path.open("rb"), size, digest)
        except DistributionError:
            raise
        except Exception as error:
            raise DistributionError("distribution.object_unavailable", "NAS cache object is unavailable") from error

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        declared = self._manifests.get(artifact_set_sha256) or self._load_manifest(artifact_set_sha256)
        expected = tuple(item for item in objects if item.kind == "model")
        return declared == expected

    def objects_for_set(self, artifact_set_sha256: str) -> tuple[DistributionObject, ...]:
        """Return the verified complete model manifest for assignment creation."""
        return self._manifests.get(artifact_set_sha256) or self._load_manifest(artifact_set_sha256)

    def verified_model_objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[dict[str, object], ...]:
        """Return receipts keyed by canonical model identity and file ID."""
        digest = artifact_set_sha256
        if digest not in self._receipts:
            if hasattr(self, "_service"):
                try:
                    self._load_manifest(digest)
                except Exception as error:
                    raise DistributionError(
                        "distribution.model_set_identity_unavailable",
                        "NAS cache manifest lacks canonical model-file identity",
                    ) from error
            else:
                raise DistributionError(
                    "distribution.model_set_identity_unavailable",
                    "NAS cache manifest lacks canonical model-file identity",
                )
        receipts = self._receipts.get(digest)
        if receipts is None:
            raise DistributionError(
                "distribution.model_set_identity_unavailable",
                "NAS cache manifest lacks canonical model-file identity",
            )
        return receipts


class CompositeVerifiedObjectSource:
    """Join the NAS model cache and Controller OCI archive boundaries."""

    def __init__(
        self,
        model_source: VerifiedObjectSource,
        oci_source: VerifiedObjectSource,
    ) -> None:
        self.model_source = model_source
        self.oci_source = oci_source

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        return self.model_source.verify_artifact_set(artifact_set_sha256, objects)

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.oci_source.verify_runtime_image(image_digest, archive_sha256)

    def verified_model_objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[dict[str, object], ...]:
        resolver = getattr(self.model_source, "verified_model_objects_for_set", None)
        if resolver is None:
            raise DistributionError(
                "distribution.model_set_identity_unavailable",
                "NAS cache source lacks canonical model-file identity",
            )
        return resolver(artifact_set_sha256)

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        # Both sources are content addressed. Probe the model cache first so a
        # shared NAS object is never copied into a second Controller store.
        try:
            return self.model_source.open_verified(digest, expected_bytes)
        except DistributionError as model_error:
            try:
                return self.oci_source.open_verified(digest, expected_bytes)
            except DistributionError:
                raise model_error


class MemoryVerifiedObjectSource:
    """Small deterministic fixture source used by Controller integration tests."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.artifact_manifests: dict[str, tuple[DistributionObject, ...]] = {}
        self.runtime_images: dict[str, str] = {}

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.objects[digest] = bytes(payload)
        return digest

    def register_runtime_image(self, image_digest: str, archive_sha256: str) -> None:
        self.runtime_images[archive_sha256] = image_digest

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.runtime_images.get(archive_sha256) == image_digest

    def register_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> None:
        digest = artifact_set_sha256
        self.artifact_manifests[digest] = tuple(item for item in objects if item.kind == "model")

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        expected = tuple(item for item in objects if item.kind == "model")
        # Fixtures receive the opaque digest from the cache manifest. The
        # production NAS adapter above performs the same exact lookup.
        return self.artifact_manifests.get(artifact_set_sha256) == expected

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        payload = self.objects.get(digest)
        if payload is None or len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != digest:
            raise DistributionError("distribution.object_unavailable", "verified object digest mismatch")
        return VerifiedObject(BytesIO(payload), len(payload), digest)


class DistributionService:
    """Resolves exact assignments and serves only their declared objects."""

    def __init__(
        self,
        source: VerifiedObjectSource,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sessions: sessionmaker[Session] | None = None,
    ) -> None:
        self.source = source
        self.clock = clock
        self.sessions = sessions
        self._assignments: dict[tuple[str, str], DistributionAssignment] = {}
        self._lock = RLock()

    def attach_sessions(self, sessions: sessionmaker[Session]) -> DistributionService:
        """Bind the service to the Controller's durable assignment store."""
        if self.sessions is not None and self.sessions is not sessions:
            raise RuntimeError("distribution service is already bound to another database")
        self.sessions = sessions
        return self

    def register(self, assignment: DistributionAssignment) -> None:
        assignment = DistributionAssignment.parse(assignment.to_mapping())
        verifier = getattr(self.source, "verify_artifact_set", None)
        if verifier is None or not verifier(assignment.model_artifact_set_sha256, assignment.objects):
            raise DistributionError(
                "distribution.model_set_mismatch",
                "assignment model objects do not match a verified cache manifest",
            )
        image_verifier = getattr(self.source, "verify_runtime_image", None)
        if image_verifier is None or not image_verifier(
            assignment.oci_image_digest, assignment.oci_archive_sha256
        ):
            raise DistributionError(
                "distribution.runtime_image_mismatch",
                "assignment OCI archive does not match the verified image identity",
            )
        with self._lock:
            key = (assignment.plan_digest, assignment.node_id)
            if self.sessions is None:
                existing = self._assignments.get(key)
                if existing is not None and existing != assignment:
                    raise DistributionError("distribution.assignment_conflict", "node assignment is already bound")
                self._assignments[key] = assignment
                return
            with self.sessions.begin() as session:
                row = session.scalar(
                    select(ArtifactDistributionAssignment).where(
                        ArtifactDistributionAssignment.plan_digest == assignment.plan_digest,
                        ArtifactDistributionAssignment.node_id == assignment.node_id,
                    ).with_for_update()
                )
                if row is not None:
                    if self._from_row(row) != assignment:
                        raise DistributionError("distribution.assignment_conflict", "node assignment is already bound")
                    return
                now = self.clock()
                session.add(
                    ArtifactDistributionAssignment(
                        id=assignment.assignment_id,
                        plan_digest=assignment.plan_digest,
                        node_id=assignment.node_id,
                        generation=assignment.generation,
                        expires_at=assignment.expires_at,
                        model_artifact_set_sha256=assignment.model_artifact_set_sha256,
                        objects=[item.to_mapping() for item in assignment.objects],
                        oci_image_digest=assignment.oci_image_digest,
                        oci_archive_sha256=assignment.oci_archive_sha256,
                        state="active",
                        created_at=now,
                        updated_at=now,
                    )
                )

    @staticmethod
    def _from_row(row: ArtifactDistributionAssignment) -> DistributionAssignment:
        return DistributionAssignment.parse(
            {
                "schema_version": 2,
                "assignment_id": row.id,
                "plan_digest": row.plan_digest,
                "generation": row.generation,
                "node_id": row.node_id,
                "expires_at": row.expires_at.replace(tzinfo=UTC).isoformat() if row.expires_at.tzinfo is None else row.expires_at.isoformat(),
                "model_artifact_set_sha256": row.model_artifact_set_sha256,
                "objects": row.objects,
                "oci_image_digest": row.oci_image_digest,
                "oci_archive_sha256": row.oci_archive_sha256,
            }
        )

    def revoke(self, *, plan_digest: str, node_id: str) -> None:
        """Revoke a durable assignment; revocation is fail-closed on reads."""
        if self.sessions is None:
            with self._lock:
                self._assignments.pop((plan_digest, node_id), None)
            return
        with self.sessions.begin() as session:
            row = session.scalar(select(ArtifactDistributionAssignment).where(
                ArtifactDistributionAssignment.plan_digest == plan_digest,
                ArtifactDistributionAssignment.node_id == node_id,
            ).with_for_update())
            if row is not None:
                row.state = "revoked"
                row.revoked_at = self.clock()
                row.updated_at = self.clock()

    def authorize(self, *, node_id: str, plan_digest: str) -> DistributionAssignment:
        with self._lock:
            if self.sessions is None:
                assignment = self._assignments.get((plan_digest, node_id))
                plan_assignment = next(
                    (item for (digest, _node), item in self._assignments.items() if digest == plan_digest),
                    None,
                )
            else:
                with self.sessions() as session:
                    row = session.scalar(select(ArtifactDistributionAssignment).where(
                        ArtifactDistributionAssignment.plan_digest == plan_digest,
                        ArtifactDistributionAssignment.node_id == node_id,
                    ))
                    if row is None:
                        assignment = None
                        plan_assignment = session.scalar(select(ArtifactDistributionAssignment).where(
                            ArtifactDistributionAssignment.plan_digest == plan_digest,
                        ))
                    elif row.state != "active":
                        raise DistributionError("distribution.revoked", "assignment is no longer active")
                    else:
                        assignment = self._from_row(row)
                        plan_assignment = assignment
        if assignment is None:
            if plan_assignment is not None:
                raise DistributionError("distribution.wrong_node", "assignment is bound to another node")
            raise DistributionError("distribution.unassigned", "assignment is not available")
        if assignment.node_id != node_id:
            raise DistributionError("distribution.wrong_node", "assignment is bound to another node")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now) or assignment.expires_at <= now:
            if self.sessions is not None:
                with self.sessions.begin() as session:
                    row = session.scalar(select(ArtifactDistributionAssignment).where(
                        ArtifactDistributionAssignment.plan_digest == plan_digest,
                        ArtifactDistributionAssignment.node_id == node_id,
                    ).with_for_update())
                    if row is not None:
                        row.state = "expired"
                        row.updated_at = now
            raise DistributionError("distribution.expired", "assignment has expired")
        return assignment

    def manifest(self, *, node_id: str, plan_digest: str) -> dict[str, object]:
        return self.authorize(node_id=node_id, plan_digest=plan_digest).to_mapping()

    def open_object(self, *, node_id: str, plan_digest: str, digest: str) -> tuple[DistributionAssignment, DistributionObject, VerifiedObject]:
        assignment = self.authorize(node_id=node_id, plan_digest=plan_digest)
        object_spec = next((item for item in assignment.objects if item.sha256 == digest), None)
        if object_spec is None:
            raise DistributionError("distribution.unassigned", "object is not assigned to this node")
        try:
            opened = self.source.open_verified(digest, object_spec.bytes)
        except DistributionError:
            raise
        except Exception as error:
            raise DistributionError("distribution.object_unavailable", "verified object is unavailable") from error
        if opened.size != object_spec.bytes or opened.sha256 != digest:
            opened.stream.close()
            raise DistributionError("distribution.object_unavailable", "source returned an invalid object")
        return assignment, object_spec, opened


__all__ = [
    "CompositeVerifiedObjectSource",
    "ControllerRuntimeImageVerifiedObjectSource",
    "DistributionError",
    "DistributionService",
    "FilesystemVerifiedObjectSource",
    "MemoryVerifiedObjectSource",
    "ModelCacheVerifiedObjectSource",
    "RecipeBuildVerifiedObjectSource",
    "VerifiedObject",
    "VerifiedObjectSource",
    "artifact_set_sha256",
    "build_distribution_service",
    "build_distribution_service_from_components",
]


def build_distribution_service(
    model_source: VerifiedObjectSource,
    oci_source: VerifiedObjectSource,
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DistributionService:
    """Production construction hook used by Controller startup wiring."""
    return DistributionService(
        CompositeVerifiedObjectSource(model_source, oci_source),
        clock=clock,
        sessions=sessions,
    )


def build_distribution_service_from_components(
    model_cache: object,
    sessions: sessionmaker[Session],
    artifact_root: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DistributionService:
    """Build the production source pair from Controller startup components."""
    return build_distribution_service(
        ModelCacheVerifiedObjectSource.from_service(model_cache),
        ControllerRuntimeImageVerifiedObjectSource(sessions, artifact_root),
        sessions,
        clock=clock,
    )
