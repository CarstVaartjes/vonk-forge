"""Controller-authorized delivery of immutable model and OCI objects.

The service is intentionally backed by a small source protocol.  The NAS cache
worker can provide that protocol without this module knowing its persistence or
eviction details; recipe image storage can use the filesystem adapter below.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import BinaryIO, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionObject,
    canonical_message,
)

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from .artifact_reference_scan import require_model_sets_open
from .distribution_assignment import NodeDistributionAssignment
from .models import (
    ArtifactDistributionAssignment,
    RecipeBuild,
)
from .runtime_image_preparation import (
    IMAGE_CACHE_DIRECTORY,
    FilesystemRuntimeImageStorage,
)


class DistributionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class OpenedObject:
    """An open stored object whose name is its content address and whose size was checked."""

    stream: BinaryIO
    size: int
    sha256: str
    # Where the object sits in Controller storage. The edge serves the bytes
    # from this path once the Controller has authorized the request.
    path: Path


class ObjectSource(Protocol):
    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        """Open a complete immutable object or raise DistributionError."""
        ...

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        """Prove that the exact model objects belong to the cache manifest."""
        ...

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        """Prove the archive is the exact OCI image selected by the plan."""
        ...


def artifact_set_sha256(objects: tuple[DistributionObject, ...]) -> str:
    """Return the digest used by cache adapters for an exact model manifest."""
    model_objects = [item.to_mapping() for item in objects if item.kind == "model"]
    return hashlib.sha256(canonical_message(model_objects)).hexdigest()


_artifact_set_digest = artifact_set_sha256


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
                "distribution.object_invalid", "object digest is invalid"
            )
        if not 1 <= expected_bytes <= self.maximum_bytes:
            raise DistributionError(
                "distribution.object_invalid", "object length is invalid"
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
                raise OSError("unsafe object root")
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
                raise DistributionError(
                    "distribution.object_unavailable", "stored object length changed"
                )
            # The name is the digest and the object entered the store through an
            # ingress that verified it; serving checks identity and size only.
            source = os.fdopen(fd, "rb", closefd=True)
            return OpenedObject(source, expected_bytes, digest, root / digest)
        except DistributionError:
            raise
        except OSError as error:
            raise DistributionError(
                "distribution.object_unavailable", "stored object is unavailable"
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
        with self.sessions() as session:
            return (
                session.scalar(
                    select(RecipeBuild.id).where(
                        RecipeBuild.state == "succeeded",
                        RecipeBuild.image_digest == image_digest,
                        RecipeBuild.oci_layout_sha256 == archive_sha256,
                        RecipeBuild.image_bytes > 0,
                    )
                )
                is not None
            )

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        with self.sessions() as session:
            authorized = session.scalar(
                select(RecipeBuild.id).where(
                    RecipeBuild.state == "succeeded",
                    RecipeBuild.oci_layout_sha256 == digest,
                    RecipeBuild.image_bytes == expected_bytes,
                )
            )
        if authorized is None:
            raise DistributionError(
                "distribution.object_unavailable",
                "OCI archive is not a succeeded build artifact",
            )
        return super().open_object(digest, expected_bytes)


class ModelCacheObjectSource:
    """Narrow adapter for the NAS model-cache verified-object service.

    ``manifests`` is keyed by the cache service's own artifact-set digest and
    contains the complete ordered model manifest.  This adapter never derives
    or rewrites that digest; the cache worker remains its authority.
    """

    # ``_service`` stays an opaque duck-typed component: the NAS worker's
    # verified-object service is not importable from this module.
    _service: object
    _manifests: dict[str, tuple[DistributionObject, ...]]
    _receipts: dict[str, tuple[dict[str, object], ...]]
    _paths: dict[str, tuple[str, str, object]]
    _open_object: Callable[[str, int], OpenedObject]

    def __init__(
        self,
        open_object: Callable[[str, int], OpenedObject],
        manifests: dict[str, tuple[DistributionObject, ...]],
    ) -> None:
        self._open_object = open_object
        self._metadata_guard = Lock()
        self._paths = {}
        self._manifests = dict(manifests)
        self._receipts: dict[str, tuple[dict[str, object], ...]] = {}

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        return self._open_object(digest, expected_bytes)

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return False

    @classmethod
    def from_service(cls, service: object) -> ModelCacheObjectSource:
        """Construct directly from the NAS worker's verified-object service."""
        return cls._from_cache_service(service)

    @classmethod
    def _from_cache_service(cls, service: object) -> ModelCacheObjectSource:
        adapter = cls.__new__(cls)
        adapter._service = service
        adapter._metadata_guard = Lock()
        adapter._manifests = {}
        adapter._receipts = {}
        adapter._paths = {}
        adapter._open_object = adapter._open_cache_object
        return adapter

    def _load_manifest(self, digest: str) -> tuple[DistributionObject, ...]:
        objects, receipts, paths = self._describe(digest)
        # Stage every descriptor before making authorization visible. The guard
        # protects only process-local metadata; cache/SQL/file IO occurs outside.
        with self._metadata_guard:
            self._paths.update(paths)
            self._manifests[digest] = objects
            if len(receipts) == len(objects):
                self._receipts[digest] = receipts
            else:
                self._receipts.pop(digest, None)
        return objects

    def _describe(
        self, digest: str, requested: object = None
    ) -> tuple[
        tuple[DistributionObject, ...],
        tuple[dict[str, object], ...],
        dict[str, tuple[str, str, object]],
    ]:
        try:
            # ModelCacheService validates its opaque digest against the full
            # canonical ArtifactSetManifest before exposing descriptors.
            manifest_provider = getattr(
                self._service, "manifest_for_artifact_set", None
            )
            descriptor_provider = getattr(
                self._service, "resolve_verified_artifact_set", None
            )
            if not isinstance(manifest_provider, Callable) or not isinstance(
                descriptor_provider, Callable
            ):
                raise TypeError("NAS cache manifest provider is unavailable")
            # The caller's own resolved manifest may name a set no row
            # records yet; the cache checks it covers verified objects.
            manifest = requested if requested is not None else manifest_provider(digest)
            if getattr(manifest, "digest", None) != digest:
                raise ValueError("cache manifest identity changed")
            descriptors = (
                descriptor_provider(digest)
                if requested is None
                else descriptor_provider(digest, manifest=requested)
            )
        except Exception as error:
            raise DistributionError(
                "distribution.model_set_mismatch", "NAS cache manifest is unavailable"
            ) from error
        objects = []
        receipts = []
        paths = {}
        for descriptor in descriptors:
            try:
                item = DistributionObject(
                    name=str(descriptor["path"]),
                    sha256=str(descriptor["sha256"]),
                    bytes=int(descriptor["bytes"]),
                    kind="model",
                )
                path = descriptor["file"]
            except (KeyError, TypeError, ValueError) as error:
                raise DistributionError(
                    "distribution.model_set_mismatch", "NAS cache manifest is malformed"
                ) from error
            objects.append(item)
            paths[item.sha256] = (digest, item.name, path)
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
        return tuple(objects), tuple(receipts), paths

    def _open_cache_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        with self._metadata_guard:
            entry = self._paths.get(digest)
        if entry is None:
            raise DistributionError(
                "distribution.object_unavailable", "NAS cache object was not authorized"
            )
        set_digest, path, _ = entry
        try:
            file_provider = getattr(self._service, "cached_artifact_file", None)
            if not isinstance(file_provider, Callable):
                raise TypeError("NAS cache object provider is unavailable")
            verified_path, size, verified_digest = file_provider(
                set_digest, digest, path
            )
            if size != expected_bytes or verified_digest != digest:
                raise DistributionError(
                    "distribution.object_unavailable",
                    "NAS cache object identity changed",
                )
            return OpenedObject(verified_path.open("rb"), size, digest, verified_path)
        except DistributionError:
            raise
        except Exception as error:
            raise DistributionError(
                "distribution.object_unavailable", "NAS cache object is unavailable"
            ) from error

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        declared = self.objects_for_set(artifact_set_sha256)
        expected = tuple(item for item in objects if item.kind == "model")
        return declared == expected

    def objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[DistributionObject, ...]:
        """Return the verified complete model manifest for assignment creation."""
        with self._metadata_guard:
            cached = self._manifests.get(artifact_set_sha256)
        return (
            cached if cached is not None else self._load_manifest(artifact_set_sha256)
        )

    def verified_model_objects_for_set(
        self, artifact_set_sha256: str, manifest: object = None
    ) -> tuple[dict[str, object], ...]:
        """Return receipts keyed by canonical model identity and file ID.

        With the caller's resolved ``manifest``, the shared verified set is
        described with that caller's model identities (see the model cache).
        """
        digest = artifact_set_sha256
        if manifest is not None and hasattr(self, "_service"):
            objects, receipts, paths = self._describe(digest, manifest)
            if len(receipts) != len(objects):
                raise DistributionError(
                    "distribution.model_set_identity_unavailable",
                    "NAS cache manifest lacks canonical model-file identity",
                )
            with self._metadata_guard:
                self._paths.update(paths)
            return receipts
        with self._metadata_guard:
            cached_receipts = self._receipts.get(digest)
        if cached_receipts is None:
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
        with self._metadata_guard:
            receipts = self._receipts.get(digest)
        if receipts is None:
            raise DistributionError(
                "distribution.model_set_identity_unavailable",
                "NAS cache manifest lacks canonical model-file identity",
            )
        return receipts


class CompositeObjectSource:
    """Join the NAS model cache and Controller OCI archive boundaries."""

    def __init__(
        self,
        model_source: ObjectSource,
        oci_source: ObjectSource,
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
        self, artifact_set_sha256: str, manifest: object = None
    ) -> tuple[dict[str, object], ...]:
        resolver = getattr(self.model_source, "verified_model_objects_for_set", None)
        if resolver is None:
            raise DistributionError(
                "distribution.model_set_identity_unavailable",
                "NAS cache source lacks canonical model-file identity",
            )
        if manifest is None:
            return resolver(artifact_set_sha256)
        return resolver(artifact_set_sha256, manifest)

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        # Both sources are content addressed. Probe the model cache first so a
        # shared NAS object is never copied into a second Controller store.
        try:
            return self.model_source.open_object(digest, expected_bytes)
        except DistributionError as model_error:
            try:
                return self.oci_source.open_object(digest, expected_bytes)
            except DistributionError:
                raise model_error


class MemoryObjectSource:
    """Small deterministic fixture source used by Controller integration tests."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        # Objects also live as files, because the edge serves stored bytes by path.
        self._directory = TemporaryDirectory(prefix="vonk-objects-")
        self.root = Path(self._directory.name)
        self.objects = dict(objects or {})
        for digest, payload in self.objects.items():
            (self.root / digest).write_bytes(payload)
        self.artifact_manifests: dict[str, tuple[DistributionObject, ...]] = {}
        self.runtime_images: dict[str, str] = {}

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.objects[digest] = bytes(payload)
        (self.root / digest).write_bytes(payload)
        return digest

    def register_runtime_image(self, image_digest: str, archive_sha256: str) -> None:
        self.runtime_images[archive_sha256] = image_digest

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.runtime_images.get(archive_sha256) == image_digest

    def register_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> None:
        digest = artifact_set_sha256
        self.artifact_manifests[digest] = tuple(
            item for item in objects if item.kind == "model"
        )

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        expected = tuple(item for item in objects if item.kind == "model")
        # Fixtures receive the opaque digest from the cache manifest. The
        # production NAS adapter above performs the same exact lookup.
        return self.artifact_manifests.get(artifact_set_sha256) == expected

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        payload = self.objects.get(digest)
        if (
            payload is None
            or len(payload) != expected_bytes
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise DistributionError(
                "distribution.object_unavailable", "verified object digest mismatch"
            )
        return OpenedObject(BytesIO(payload), len(payload), digest, self.root / digest)


_AUTHORIZATION_CACHE_ENTRIES = 1024
_AUTHORIZATION_TTL_SECONDS = 30.0


def _may_replace(
    existing: NodeDistributionAssignment,
    requested: NodeDistributionAssignment,
    *,
    active: bool,
    now: datetime,
) -> bool:
    """Whether a registration may take over a stored (plan, node) grant.

    The same grant (equal bytes and identities; only its expiry differs) is
    renewed by whichever switch now transfers it. A grant that is no longer
    live -- revoked, expired, or past its expiry -- belongs to no transfer and
    is reclaimed. A live grant for different bytes stays refused.
    """

    def grant(value: NodeDistributionAssignment) -> dict[str, object]:
        mapping = value.to_mapping()
        mapping.pop("expires_at", None)
        return mapping

    return grant(existing) == grant(requested) or not (
        active and existing.expires_at > now
    )


class DistributionService:
    """Resolves exact assignments and serves only their declared objects."""

    def __init__(
        self,
        source: ObjectSource,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sessions: sessionmaker[Session] | None = None,
    ) -> None:
        self.source = source
        self.clock = clock
        self.sessions = sessions
        # In-memory mode (no database) is a test double. Its dict needs a lock
        # for check-then-write in register/revoke only; database mode takes no
        # process-wide lock because Postgres arbitrates concurrent access.
        self._assignments: dict[tuple[str, str], NodeDistributionAssignment] = {}
        self._lock = Lock()
        # Authorization is decided per assignment, not per range request.
        # Bounded and short-lived so a revocation made by another process is
        # seen within _AUTHORIZATION_TTL_SECONDS (eventually consistent).
        self._authorized: OrderedDict[
            tuple[str, str], tuple[NodeDistributionAssignment, float]
        ] = OrderedDict()
        self._authorized_lock = Lock()  # guards only the dict, never I/O

    def attach_sessions(self, sessions: sessionmaker[Session]) -> DistributionService:
        """Bind the service to the Controller's durable assignment store."""
        if self.sessions is not None and self.sessions is not sessions:
            raise RuntimeError(
                "distribution service is already bound to another database"
            )
        self.sessions = sessions
        return self

    def register(self, assignment: NodeDistributionAssignment) -> None:
        assignment = NodeDistributionAssignment.parse(assignment.to_mapping())
        verifier = getattr(self.source, "verify_artifact_set", None)
        if verifier is None or not verifier(
            assignment.model_artifact_set_sha256, assignment.objects
        ):
            raise DistributionError(
                "distribution.model_set_mismatch",
                "assignment model objects do not match a verified cache manifest",
            )
        image_verifier = getattr(self.source, "verify_runtime_image", None)
        image_verified = image_verifier is not None and image_verifier(
            assignment.oci_image_digest, assignment.oci_archive_sha256
        )
        if not image_verified:
            raise DistributionError(
                "distribution.runtime_image_mismatch",
                "assignment OCI archive does not match the verified image identity",
            )
        key = (assignment.plan_digest, assignment.node_id)
        with self._authorized_lock:
            self._authorized.pop(key, None)
        if self.sessions is None:
            with self._lock:
                existing = self._assignments.get(key)
                if existing is not None and not _may_replace(
                    existing, assignment, active=True, now=self.clock()
                ):
                    raise DistributionError(
                        "distribution.assignment_conflict",
                        "node assignment is already bound",
                    )
                self._assignments[key] = assignment
            return
        try:
            self._register_row(assignment)
        except IntegrityError:
            # A concurrent identical registration won the unique
            # (plan_digest, node_id) constraint; the second pass compares.
            self._register_row(assignment)

    def _register_row(self, assignment: NodeDistributionAssignment) -> None:
        sessions = self.sessions
        assert sessions is not None
        with sessions.begin() as session:
            row = session.scalar(
                select(ArtifactDistributionAssignment)
                .where(
                    ArtifactDistributionAssignment.plan_digest
                    == assignment.plan_digest,
                    ArtifactDistributionAssignment.node_id == assignment.node_id,
                )
                .with_for_update()
            )
            now = self.clock()
            if row is not None:
                existing = self._from_row(row)
                if existing == assignment and row.state == "active":
                    return
                if not _may_replace(
                    existing, assignment, active=row.state == "active", now=now
                ):
                    raise DistributionError(
                        "distribution.assignment_conflict",
                        "node assignment is already bound",
                    )
            try:
                require_model_sets_open(
                    session,
                    (assignment.model_artifact_set_sha256,),
                    now=now,
                )
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", assignment.oci_archive_sha256),),
                    now=now,
                )
            except ArtifactLifecycleError as error:
                raise DistributionError(error.code, error.detail) from error
            if row is not None and row.id != assignment.assignment_id:
                # Reclaim creates a new grant identity. Delete the expired or
                # revoked grant under the plan/node lock, then insert its
                # successor in this transaction; primary keys never change.
                session.delete(row)
                session.flush()
                row = None
            if row is not None:
                row.generation = assignment.generation
                row.expires_at = assignment.expires_at
                row.model_artifact_set_sha256 = assignment.model_artifact_set_sha256
                row.objects = [item.to_mapping() for item in assignment.objects]
                row.oci_image_digest = assignment.oci_image_digest
                row.oci_archive_sha256 = assignment.oci_archive_sha256
                row.state = "active"
                row.revoked_at = None
                row.updated_at = now
                return
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
    def _from_row(row: ArtifactDistributionAssignment) -> NodeDistributionAssignment:
        return NodeDistributionAssignment.parse(
            {
                "assignment_id": row.id,
                "plan_digest": row.plan_digest,
                "generation": row.generation,
                "node_id": row.node_id,
                "expires_at": row.expires_at.replace(tzinfo=UTC).isoformat()
                if row.expires_at.tzinfo is None
                else row.expires_at.isoformat(),
                "model_artifact_set_sha256": row.model_artifact_set_sha256,
                "objects": row.objects,
                "oci_image_digest": row.oci_image_digest,
                "oci_archive_sha256": row.oci_archive_sha256,
            }
        )

    def revoke(self, *, plan_digest: str, node_id: str) -> None:
        """Revoke a durable assignment; revocation is fail-closed on reads."""
        with self._authorized_lock:
            self._authorized.pop((plan_digest, node_id), None)
        if self.sessions is None:
            with self._lock:
                self._assignments.pop((plan_digest, node_id), None)
            return
        with self.sessions.begin() as session:
            row = session.scalar(
                select(ArtifactDistributionAssignment)
                .where(
                    ArtifactDistributionAssignment.plan_digest == plan_digest,
                    ArtifactDistributionAssignment.node_id == node_id,
                )
                .with_for_update()
            )
            if row is not None:
                row.state = "revoked"
                row.revoked_at = self.clock()
                row.updated_at = self.clock()

    def authorize(
        self, *, node_id: str, plan_digest: str
    ) -> NodeDistributionAssignment:
        """Decide once per assignment, not once per range request.

        A positive decision is remembered for a short, bounded time and never
        past the assignment's own expiry. There is no process-wide lock: the
        database arbitrates, and the cache only holds immutable assignments.
        """
        key = (plan_digest, node_id)
        now = self.clock()
        with self._authorized_lock:
            cached = self._authorized.get(key)
            if cached is not None:
                assignment, decided_at = cached
                if (
                    monotonic() - decided_at < _AUTHORIZATION_TTL_SECONDS
                    and assignment.expires_at > now
                ):
                    return assignment
                del self._authorized[key]
        assignment = self._authorize_uncached(node_id=node_id, plan_digest=plan_digest)
        with self._authorized_lock:
            self._authorized[key] = (assignment, monotonic())
            while len(self._authorized) > _AUTHORIZATION_CACHE_ENTRIES:
                self._authorized.popitem(last=False)
        return assignment

    def _authorize_uncached(
        self, *, node_id: str, plan_digest: str
    ) -> NodeDistributionAssignment:
        if self.sessions is None:
            assignment = self._assignments.get((plan_digest, node_id))
            plan_assignment = next(
                (
                    item
                    for (digest, _node), item in list(self._assignments.items())
                    if digest == plan_digest
                ),
                None,
            )
        else:
            with self.sessions() as session:
                row = session.scalar(
                    select(ArtifactDistributionAssignment).where(
                        ArtifactDistributionAssignment.plan_digest == plan_digest,
                        ArtifactDistributionAssignment.node_id == node_id,
                    )
                )
                if row is None:
                    assignment = None
                    plan_assignment = session.scalar(
                        select(ArtifactDistributionAssignment).where(
                            ArtifactDistributionAssignment.plan_digest == plan_digest,
                        )
                    )
                elif row.state != "active":
                    raise DistributionError(
                        "distribution.revoked", "assignment is no longer active"
                    )
                else:
                    assignment = self._from_row(row)
                    plan_assignment = assignment
        if assignment is None:
            if plan_assignment is not None:
                raise DistributionError(
                    "distribution.wrong_node", "assignment is bound to another node"
                )
            raise DistributionError(
                "distribution.unassigned", "assignment is not available"
            )
        if assignment.node_id != node_id:
            raise DistributionError(
                "distribution.wrong_node", "assignment is bound to another node"
            )
        now = self.clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
            or assignment.expires_at <= now
        ):
            if self.sessions is not None:
                with self.sessions.begin() as session:
                    row = session.scalar(
                        select(ArtifactDistributionAssignment)
                        .where(
                            ArtifactDistributionAssignment.plan_digest == plan_digest,
                            ArtifactDistributionAssignment.node_id == node_id,
                        )
                        .with_for_update()
                    )
                    if row is not None:
                        row.state = "expired"
                        row.updated_at = now
            raise DistributionError("distribution.expired", "assignment has expired")
        return assignment

    def open_object(
        self, *, node_id: str, plan_digest: str, digest: str
    ) -> tuple[NodeDistributionAssignment, DistributionObject, OpenedObject]:
        assignment = self.authorize(node_id=node_id, plan_digest=plan_digest)
        object_spec = next(
            (item for item in assignment.objects if item.sha256 == digest), None
        )
        if object_spec is None:
            raise DistributionError(
                "distribution.unassigned", "object is not assigned to this node"
            )
        # The worker registers assignments, while another API process serves
        # their bytes. Rehydrate that process's model lookup from the durable
        # assignment instead of relying on the worker's in-memory cache.
        if object_spec.kind == "model" and not self.source.verify_artifact_set(
            assignment.model_artifact_set_sha256, assignment.objects
        ):
            raise DistributionError(
                "distribution.model_set_mismatch",
                "assignment model objects do not match the cache manifest",
            )
        try:
            opened = self.source.open_object(digest, object_spec.bytes)
        except DistributionError:
            raise
        except Exception as error:
            raise DistributionError(
                "distribution.object_unavailable", "stored object is unavailable"
            ) from error
        if opened.size != object_spec.bytes or opened.sha256 != digest:
            opened.stream.close()
            raise DistributionError(
                "distribution.object_unavailable", "source returned an invalid object"
            )
        return assignment, object_spec, opened


__all__ = [
    "CompositeObjectSource",
    "DistributionError",
    "DistributionService",
    "FilesystemObjectSource",
    "MemoryObjectSource",
    "ModelCacheObjectSource",
    "ObjectSource",
    "OpenedObject",
    "RecipeBuildObjectSource",
    "artifact_set_sha256",
    "build_distribution_service",
    "build_distribution_service_from_components",
]


def build_distribution_service(
    model_source: ObjectSource,
    oci_source: ObjectSource,
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DistributionService:
    """Production construction hook used by Controller startup wiring."""
    return DistributionService(
        CompositeObjectSource(model_source, oci_source),
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
        ModelCacheObjectSource.from_service(model_cache),
        RecipeBuildObjectSource(sessions, artifact_root),
        sessions,
        clock=clock,
    )
